"""
Benchmark suite for QgisMCPClient socket operations.

Measures performance of the hot paths in the socket client:
- _recv_exact() with various payload sizes
- send_command() serialization/deserialization overhead
- settimeout() syscall overhead
- JSON encode vs decode for typical payloads
- Socket send strategies (concatenated vs separate sends)

Run: uv run --no-sync python benchmarks/bench_socket_client.py

Plugin-side socket handling bottlenecks (static review of qgis_mcp_plugin.py):
  1. _send_response() concatenates header + resp_bytes before sendall(). For large
     responses (base64 images ~5MB), this allocates a new buffer of header_len + data_len.
     Same issue as client-side send_command().
  2. process_server() uses `self.buffer += data` (bytes concatenation) which creates a
     new bytes object on every recv(). For messages that arrive in many chunks, this is
     O(n^2) in total bytes received. Should use bytearray or buffer protocol instead.
  3. process_server() slices buffer with `self.buffer[4:4 + msg_len]` and then
     `self.buffer = self.buffer[4 + msg_len:]`, creating two new bytes objects per
     message. A memoryview + offset tracking approach would avoid these copies.
  4. No TCP_NODELAY on the accepted client socket. Each small response (ping, get_info)
     may be delayed by Nagle's algorithm up to 40ms before being sent.
  5. The QTimer poll interval is 25ms, meaning there's up to 25ms latency before the
     plugin even reads the incoming command. This is the dominant latency for small
     commands but is a deliberate design choice to avoid blocking the QGIS event loop.
  6. json.dumps(response).encode('utf-8') in _send_response() could benefit from
     writing directly to a buffer, though Python's json module doesn't support this
     natively without orjson.
"""

import base64
import json
import os
import socket
import struct
import sys
import time

# Add project src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qgis_mcp.client import QgisMCPClient

# ---------------------------------------------------------------------------
# Payload generators
# ---------------------------------------------------------------------------


def make_feature_payload(n: int = 50) -> dict:
    """Typical get_layer_features response with n features."""
    features = [
        {"_fid": i, "name": f"Feature_{i}", "value": i * 1.5, "category": "A"} for i in range(n)
    ]
    return {
        "status": "success",
        "result": {
            "features": features,
            "feature_count": n,
            "fields": ["name", "value", "category"],
        },
    }


def make_layer_list_payload(n: int = 100) -> dict:
    """Typical get_layers response."""
    layers = [
        {"id": f"layer_{i}", "name": f"Layer {i}", "type": "vector_1", "crs": "EPSG:4326"}
        for i in range(n)
    ]
    return {
        "status": "success",
        "result": {"layers": layers, "total_count": n, "offset": 0, "limit": n},
    }


def make_base64_image_payload(size_kb: int) -> dict:
    """Simulate render_map / get_canvas_screenshot with base64 image data."""
    raw = os.urandom(size_kb * 1024)
    b64 = base64.b64encode(raw).decode("ascii")
    return {
        "status": "success",
        "result": {
            "base64_data": b64,
            "mime_type": "image/png",
            "width": 800,
            "height": 600,
        },
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_socket_pair():
    """Create a connected pair of TCP sockets via loopback for realistic benchmarks."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))
    peer_sock, _ = srv.accept()
    srv.close()
    return client_sock, peer_sock


def timeit(func, *, iterations: int = 1000, label: str = "") -> float:
    """Run func() for `iterations` times, return median time in microseconds."""
    times = []
    # Warmup
    for _ in range(min(50, iterations)):
        func()
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        func()
        t1 = time.perf_counter_ns()
        times.append(t1 - t0)
    times.sort()
    median_ns = times[len(times) // 2]
    median_us = median_ns / 1000
    p99_us = times[int(len(times) * 0.99)] / 1000
    if label:
        print(
            f"  {label}: median={median_us:.1f}us  p99={p99_us:.1f}us  ({iterations} iters)",
            flush=True,
        )
    return median_us


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def bench_recv_exact():
    """Benchmark _recv_exact() with different payload sizes."""
    import threading

    _print("\n=== _recv_exact() performance ===")
    sizes = [
        ("1 KB", 1024),
        ("100 KB", 100 * 1024),
        ("1 MB", 1024 * 1024),
        ("5 MB (base64 image)", 5 * 1024 * 1024),
    ]

    for label, size in sizes:
        client_sock, peer_sock = make_mock_socket_pair()
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client = QgisMCPClient()
        client.socket = client_sock

        payload = b"x" * size
        frame = struct.pack(">I", len(payload)) + payload
        use_thread = size > 100 * 1024
        iters = 200 if size <= 100 * 1024 else (20 if size <= 1024 * 1024 else 10)

        def run():
            if use_thread:
                t = threading.Thread(target=peer_sock.sendall, args=(frame,), daemon=True)
                t.start()
                header = client._recv_exact(4)
                length = struct.unpack(">I", header)[0]
                client._recv_exact(length)
                t.join()
            else:
                peer_sock.sendall(frame)
                header = client._recv_exact(4)
                length = struct.unpack(">I", header)[0]
                client._recv_exact(length)

        timeit(run, iterations=iters, label=f"recv_exact {label}")

        client_sock.close()
        peer_sock.close()


def bench_json_serde():
    """Benchmark JSON serialization/deserialization for typical payloads."""
    _print("\n=== JSON encode/decode ===")
    payloads = [
        ("ping response", {"status": "success", "result": {"pong": True}}),
        ("50 features", make_feature_payload(50)),
        ("100 layers", make_layer_list_payload(100)),
        ("100KB base64 image", make_base64_image_payload(100)),
        ("1MB base64 image", make_base64_image_payload(1024)),
    ]

    for label, payload in payloads:
        json_str = json.dumps(payload)
        json_bytes = json_str.encode("utf-8")

        timeit(
            lambda p=payload: json.dumps(p).encode("utf-8"),
            iterations=500,
            label=f"encode {label} ({len(json_bytes)} bytes)",
        )
        timeit(
            lambda b=json_bytes: json.loads(b.decode("utf-8")),
            iterations=500,
            label=f"decode {label} ({len(json_bytes)} bytes)",
        )
        # Also test json.loads directly on bytes (avoids .decode())
        timeit(
            lambda b=json_bytes: json.loads(b),
            iterations=500,
            label=f"decode-bytes {label} ({len(json_bytes)} bytes)",
        )


def bench_settimeout_overhead():
    """Measure the cost of settimeout() syscalls."""
    _print("\n=== settimeout() overhead ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    timeit(
        lambda: sock.settimeout(30),
        iterations=10000,
        label="settimeout(30)",
    )
    timeit(
        lambda: sock.settimeout(None),
        iterations=10000,
        label="settimeout(None)",
    )
    # For comparison: gettimeout() is a pure Python attribute read
    timeit(
        lambda: sock.gettimeout(),
        iterations=10000,
        label="gettimeout()",
    )
    sock.close()


def bench_send_strategies():
    """Compare header+data concatenation vs two separate sendall() calls."""
    import threading

    _print("\n=== Send strategies (header concat vs separate sends) ===")
    sizes = [
        ("small (100B ping)", 100),
        ("medium (10KB features)", 10 * 1024),
        ("large (1MB image)", 1024 * 1024),
    ]

    for label, size in sizes:
        data = b"x" * size
        header = struct.pack(">I", size)
        use_thread = size > 100 * 1024
        iters = 200 if size <= 100 * 1024 else 20

        def _drain(sock, n):
            remaining = n
            while remaining > 0:
                chunk = sock.recv(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)

        # Strategy 1: concatenate then send
        client_sock1, peer_sock1 = make_mock_socket_pair()

        def concat_send():
            if use_thread:
                t = threading.Thread(target=_drain, args=(peer_sock1, size + 4), daemon=True)
                t.start()
                client_sock1.sendall(header + data)
                t.join()
            else:
                client_sock1.sendall(header + data)
                peer_sock1.recv(size + 4)

        timeit(concat_send, iterations=iters, label=f"concat sendall {label}")
        client_sock1.close()
        peer_sock1.close()

        # Strategy 2: two separate sendall calls
        client_sock2, peer_sock2 = make_mock_socket_pair()

        def separate_send():
            if use_thread:
                t = threading.Thread(target=_drain, args=(peer_sock2, size + 4), daemon=True)
                t.start()
                client_sock2.sendall(header)
                client_sock2.sendall(data)
                t.join()
            else:
                client_sock2.sendall(header)
                client_sock2.sendall(data)
                peer_sock2.recv(size + 4)

        timeit(separate_send, iterations=iters, label=f"two sendall {label}")
        client_sock2.close()
        peer_sock2.close()


def bench_send_command_e2e():
    """End-to-end send_command() with mock socket, measuring full serde + I/O."""
    _print("\n=== send_command() end-to-end ===")
    import threading

    payloads = [
        ("ping", {}, {"status": "success", "result": {"pong": True}}),
        ("get_layers", {"limit": 50}, make_layer_list_payload(50)),
        ("get_layer_features", {"layer_id": "x", "limit": 10}, make_feature_payload(10)),
        ("render_map (1MB)", {"width": 800}, make_base64_image_payload(1024)),
    ]

    for label, params, response in payloads:
        client_sock, peer_sock = make_mock_socket_pair()
        # Set TCP_NODELAY like connect() does, since we bypass connect() here
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client = QgisMCPClient()
        client.socket = client_sock

        resp_bytes = json.dumps(response).encode("utf-8")
        resp_header = struct.pack(">I", len(resp_bytes))
        resp_frame = resp_header + resp_bytes

        peer_sock.settimeout(5)

        def echo_server():
            """Read request, send canned response."""
            while True:
                try:
                    # Read request header
                    hdr = b""
                    while len(hdr) < 4:
                        chunk = peer_sock.recv(4 - len(hdr))
                        if not chunk:
                            return
                        hdr += chunk
                    req_len = struct.unpack(">I", hdr)[0]
                    # Read request body (discard)
                    remaining = req_len
                    while remaining > 0:
                        chunk = peer_sock.recv(min(remaining, 65536))
                        if not chunk:
                            return
                        remaining -= len(chunk)
                    # Send canned response
                    peer_sock.sendall(resp_frame)
                except (TimeoutError, OSError):
                    return

        t = threading.Thread(target=echo_server, daemon=True)
        t.start()

        iters = 100 if len(resp_bytes) > 500_000 else 500
        timeit(
            lambda: client.send_command(label, params, timeout=30),
            iterations=iters,
            label=f"send_command '{label}' (resp={len(resp_bytes)} bytes)",
        )

        client_sock.close()
        peer_sock.close()
        t.join(timeout=2)


def bench_recv_exact_memoryview():
    """Compare old bytearray.extend() vs new recv_into() + memoryview approach."""
    import threading

    _print("\n=== _recv_exact(): bytearray.extend vs recv_into+memoryview ===")

    def recv_exact_old(sock, n):
        """Original implementation."""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(min(n - len(buf), 65536))
            if not chunk:
                raise ConnectionError("Connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def recv_exact_new(sock, n):
        """Optimized: pre-allocated buffer with recv_into + memoryview."""
        buf = bytearray(n)
        view = memoryview(buf)
        pos = 0
        while pos < n:
            nbytes = sock.recv_into(view[pos:], min(n - pos, 65536))
            if nbytes == 0:
                raise ConnectionError("Connection closed")
            pos += nbytes
        return bytes(buf)

    sizes = [
        ("1 KB", 1024),
        ("100 KB", 100 * 1024),
        ("1 MB", 1024 * 1024),
        ("5 MB", 5 * 1024 * 1024),
    ]

    for label, size in sizes:
        payload = b"x" * size
        # Use fewer iterations for large payloads
        iters = 200 if size <= 100 * 1024 else (20 if size <= 1024 * 1024 else 10)

        # For payloads > socket buffer (~128KB), use a sender thread to avoid deadlock
        use_thread = size > 100 * 1024

        # Old approach
        cs1, ps1 = make_mock_socket_pair()

        def run_old():
            if use_thread:
                t = threading.Thread(target=ps1.sendall, args=(payload,), daemon=True)
                t.start()
                recv_exact_old(cs1, size)
                t.join()
            else:
                ps1.sendall(payload)
                recv_exact_old(cs1, size)

        timeit(run_old, iterations=iters, label=f"OLD extend    {label}")
        cs1.close()
        ps1.close()

        # New approach
        cs2, ps2 = make_mock_socket_pair()

        def run_new():
            if use_thread:
                t = threading.Thread(target=ps2.sendall, args=(payload,), daemon=True)
                t.start()
                recv_exact_new(cs2, size)
                t.join()
            else:
                ps2.sendall(payload)
                recv_exact_new(cs2, size)

        timeit(run_new, iterations=iters, label=f"NEW recv_into {label}")
        cs2.close()
        ps2.close()


def bench_json_loads_bytes_vs_str():
    """json.loads() can accept bytes directly -- measure the .decode() savings."""
    _print("\n=== json.loads(bytes) vs json.loads(str) ===")
    payloads = [
        ("small dict", json.dumps({"pong": True}).encode()),
        ("50 features", json.dumps(make_feature_payload(50)).encode()),
        ("1MB image", json.dumps(make_base64_image_payload(1024)).encode()),
    ]
    for label, data in payloads:
        timeit(
            lambda d=data: json.loads(d.decode("utf-8")),
            iterations=500,
            label=f"loads(decode) {label} ({len(data)} bytes)",
        )
        timeit(
            lambda d=data: json.loads(d),
            iterations=500,
            label=f"loads(bytes)  {label} ({len(data)} bytes)",
        )


def _print(msg: str = "") -> None:
    print(msg, flush=True)


if __name__ == "__main__":
    _print("=" * 70)
    _print("QGIS MCP Socket Client Benchmarks")
    _print("=" * 70)

    bench_settimeout_overhead()
    bench_json_serde()
    bench_json_loads_bytes_vs_str()
    bench_recv_exact_memoryview()
    bench_send_strategies()
    bench_recv_exact()
    bench_send_command_e2e()

    _print()
    _print("=" * 70)
    _print("Done.")
