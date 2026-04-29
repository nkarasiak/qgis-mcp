#!/usr/bin/env python3
"""
Benchmark script for QGIS MCP Server hot paths.

Measures overhead of key operations using mocked socket connections
to isolate serialization, deserialization, and framework costs from
actual QGIS I/O.

Run: uv run --no-sync python benchmarks/bench_mcp_server.py
"""

import asyncio
import json
import os
import statistics
import struct
import sys
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    name: str
    iterations: int
    total_s: float
    mean_us: float
    median_us: float
    stdev_us: float
    min_us: float
    max_us: float

    def __str__(self) -> str:
        return (
            f"  {self.name:<45} "
            f"mean={self.mean_us:>8.1f}us  "
            f"median={self.median_us:>8.1f}us  "
            f"stdev={self.stdev_us:>7.1f}us  "
            f"min={self.min_us:>7.1f}us  "
            f"max={self.max_us:>8.1f}us  "
            f"({self.iterations} iters, {self.total_s:.3f}s total)"
        )


def bench(name: str, func, iterations: int = 10_000, setup=None) -> BenchResult:
    """Run a synchronous benchmark."""
    if setup:
        setup()
    # Warmup
    for _ in range(min(100, iterations // 10)):
        func()
    # Timed
    times_ns: list[int] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        func()
        times_ns.append(time.perf_counter_ns() - t0)
    times_us = [t / 1000 for t in times_ns]
    return BenchResult(
        name=name,
        iterations=iterations,
        total_s=sum(times_ns) / 1e9,
        mean_us=statistics.mean(times_us),
        median_us=statistics.median(times_us),
        stdev_us=statistics.stdev(times_us) if len(times_us) > 1 else 0,
        min_us=min(times_us),
        max_us=max(times_us),
    )


async def async_bench(name: str, coro_factory, iterations: int = 10_000) -> BenchResult:
    """Run an async benchmark."""
    # Warmup
    for _ in range(min(100, iterations // 10)):
        await coro_factory()
    # Timed
    times_ns: list[int] = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        await coro_factory()
        times_ns.append(time.perf_counter_ns() - t0)
    times_us = [t / 1000 for t in times_ns]
    return BenchResult(
        name=name,
        iterations=iterations,
        total_s=sum(times_ns) / 1e9,
        mean_us=statistics.mean(times_us),
        median_us=statistics.median(times_us),
        stdev_us=statistics.stdev(times_us) if len(times_us) > 1 else 0,
        min_us=min(times_us),
        max_us=max(times_us),
    )


def _make_ctx():
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.elicit = AsyncMock(side_effect=Exception("Elicitation not supported"))
    return ctx


# ---------------------------------------------------------------------------
# Payloads for JSON serialization benchmarks
# ---------------------------------------------------------------------------

SMALL_PAYLOAD = {"pong": True}

MEDIUM_PAYLOAD = {
    "layers": [
        {
            "id": f"layer_{i}",
            "name": f"Layer {i}",
            "type": "vector_1",
            "visible": True,
            "feature_count": i * 100,
            "crs": "EPSG:4326",
        }
        for i in range(20)
    ],
    "total_count": 20,
    "offset": 0,
    "limit": 50,
}

LARGE_FEATURES_PAYLOAD = {
    "features": [
        {
            "_fid": i,
            "name": f"Feature {i}",
            "population": i * 1000,
            "area": i * 1.5,
            "category": f"cat_{i % 5}",
            "_geometry": f"POINT({i} {i * 0.5})",
        }
        for i in range(50)
    ],
    "feature_count": 50,
    "fields": ["name", "population", "area", "category"],
}


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


def bench_json_serialization() -> list[BenchResult]:
    """Measure JSON serialization overhead for typical payloads."""
    results = []
    for name, payload in [
        ("json.dumps: small (ping)", SMALL_PAYLOAD),
        ("json.dumps: medium (20 layers)", MEDIUM_PAYLOAD),
        ("json.dumps: large (50 features)", LARGE_FEATURES_PAYLOAD),
    ]:
        data = payload
        results.append(bench(name, lambda d=data: json.dumps(d)))

    # Also measure json.loads (deserialization)
    for name, payload in [
        ("json.loads: small (ping)", SMALL_PAYLOAD),
        ("json.loads: medium (20 layers)", MEDIUM_PAYLOAD),
        ("json.loads: large (50 features)", LARGE_FEATURES_PAYLOAD),
    ]:
        encoded = json.dumps(payload)
        results.append(bench(name, lambda e=encoded: json.loads(e)))

    return results


def bench_length_prefix_framing() -> list[BenchResult]:
    """Measure struct pack/unpack for length-prefix framing."""
    results = []
    payload = json.dumps(MEDIUM_PAYLOAD).encode("utf-8")
    results.append(
        bench(
            "struct.pack: length header",
            lambda: struct.pack(">I", len(payload)),
        )
    )
    header = struct.pack(">I", len(payload))
    results.append(
        bench(
            "struct.unpack: length header",
            lambda: struct.unpack(">I", header)[0],
        )
    )
    return results


def bench_getpeername_syscall() -> list[BenchResult]:
    """Measure getpeername() overhead (simulated via mock)."""
    results = []
    mock_sock = MagicMock()
    mock_sock.getpeername.return_value = ("localhost", 9876)
    results.append(
        bench(
            "socket.getpeername() [mock]",
            lambda: mock_sock.getpeername(),
        )
    )
    return results


def bench_get_qgis_connection() -> list[BenchResult]:
    """Measure get_qgis_connection() with cached connection (happy path)."""
    import qgis_mcp.server as srv

    mock_client = MagicMock()
    mock_client.socket = MagicMock()
    mock_client.socket.getpeername.return_value = ("localhost", 9876)

    results = []

    # Benchmark: cached path (connection exists and is valid)
    def setup():
        srv._qgis_connection = mock_client

    results.append(
        bench(
            "get_qgis_connection: cached (getpeername)",
            lambda: srv.get_qgis_connection(),
            setup=setup,
        )
    )

    # Benchmark: with TTL cache (after optimization)
    if hasattr(srv, "_connection_validated_at"):

        def setup_ttl():
            srv._qgis_connection = mock_client
            srv._connection_validated_at = time.monotonic()

        results.append(
            bench(
                "get_qgis_connection: TTL-cached (no syscall)",
                lambda: srv.get_qgis_connection(),
                setup=setup_ttl,
            )
        )

    # Cleanup
    srv._qgis_connection = None
    return results


def bench_send_helper() -> list[BenchResult]:
    """Measure _send_sync() overhead with mocked socket."""
    import qgis_mcp.server as srv

    mock_client = MagicMock()
    mock_client.socket = MagicMock()
    mock_client.socket.getpeername.return_value = ("localhost", 9876)
    mock_client.send_command.return_value = {
        "status": "success",
        "result": MEDIUM_PAYLOAD,
    }

    results = []

    with patch("qgis_mcp.server.get_qgis_connection", return_value=mock_client):
        results.append(
            bench(
                "_send_sync: ping (small response)",
                lambda: srv._send_sync("ping"),
                iterations=10_000,
            )
        )
        mock_client.send_command.return_value = {
            "status": "success",
            "result": LARGE_FEATURES_PAYLOAD,
        }
        results.append(
            bench(
                "_send_sync: features (large response)",
                lambda: srv._send_sync("get_layer_features", {"layer_id": "test"}),
                iterations=10_000,
            )
        )

    return results


async def bench_tool_invocation() -> list[BenchResult]:
    """Measure full tool invocation overhead (async tool -> _send -> mock socket)."""
    from qgis_mcp.server import get_layer_features, get_layers, ping

    mock_client = MagicMock()
    mock_client.socket = MagicMock()
    mock_client.socket.getpeername.return_value = ("localhost", 9876)

    results = []
    ctx = _make_ctx()

    with patch("qgis_mcp.server.get_qgis_connection", return_value=mock_client):
        # ping — simplest tool
        mock_client.send_command.return_value = {"status": "success", "result": {"pong": True}}
        results.append(
            await async_bench(
                "tool: ping (minimal overhead)",
                lambda: ping(ctx),
                iterations=5_000,
            )
        )

        # get_layers — moderate params
        mock_client.send_command.return_value = {"status": "success", "result": MEDIUM_PAYLOAD}
        results.append(
            await async_bench(
                "tool: get_layers (medium payload)",
                lambda: get_layers(ctx, limit=50, offset=0),
                iterations=5_000,
            )
        )

        # get_layer_features — most complex params
        mock_client.send_command.return_value = {
            "status": "success",
            "result": LARGE_FEATURES_PAYLOAD,
        }
        results.append(
            await async_bench(
                "tool: get_layer_features (large payload)",
                lambda: get_layer_features(ctx, layer_id="test", limit=50),
                iterations=5_000,
            )
        )

    return results


async def bench_completion_handler() -> list[BenchResult]:
    """Measure handle_completion() overhead."""
    import qgis_mcp.server as srv

    mock_client = MagicMock()
    mock_client.socket = MagicMock()
    mock_client.socket.getpeername.return_value = ("localhost", 9876)
    mock_client.send_command.return_value = {
        "status": "success",
        "result": {
            "layers": [{"id": f"layer_{i}", "name": f"L{i}"} for i in range(50)],
            "total_count": 50,
        },
    }

    results = []

    with patch("qgis_mcp.server.get_qgis_connection", return_value=mock_client):
        ref = MagicMock()
        arg = MagicMock()
        arg.name = "layer_id"
        arg.value = ""
        results.append(
            await async_bench(
                "handle_completion: layer_id (no filter)",
                lambda: srv.handle_completion(ref, arg, None),
                iterations=2_000,
            )
        )

        arg_filtered = MagicMock()
        arg_filtered.name = "layer_id"
        arg_filtered.value = "layer_2"
        results.append(
            await async_bench(
                "handle_completion: layer_id (filtered)",
                lambda: srv.handle_completion(ref, arg_filtered, None),
                iterations=2_000,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Plugin bottleneck notes (static analysis)
# ---------------------------------------------------------------------------

PLUGIN_NOTES = """
=== QGIS Plugin Static Analysis: Bottleneck Notes ===

1. handlers dict REBUILT on every command (line ~159-216)
   - 48-entry dict literal is constructed for each execute_command() call.
   - Could be a class attribute built once at __init__ time.

2. QgsMessageLog.logMessage() on EVERY command (lines ~221-225)
   - Two log calls per command (start + complete). Logging is synchronous
     and goes through Qt signal dispatch. For high-frequency commands (e.g.,
     repeated get_layer_features during exploration), this adds overhead.
   - Consider: log at DEBUG level or make it conditional.

3. _send_response() uses json.dumps() + sendall() (line ~86-88)
   - json.dumps is called on every response. For large feature payloads
     (50 features with geometry), this can take 100-500us.
   - No response streaming: entire response is serialized in memory.

4. buffer slicing: self.buffer = self.buffer[4 + msg_len:] (line ~122)
   - Creates a new bytes object on every message. For typical single-message
     patterns this is fine, but batch commands with many messages in the
     buffer would cause O(n*m) copying.
   - Could use memoryview or a bytearray with del for large buffers.

5. render_map_base64 / get_canvas_screenshot: base64 encoding in plugin
   - Large images (1920x1080) can produce 1-5MB of base64 data that gets
     JSON-serialized. This is the heaviest response path.
   - Consider: chunked transfer or compression.

6. Timer interval: 25ms polling (line ~50)
   - Minimum latency floor of ~12.5ms average for command processing.
   - Could be lowered (e.g., 10ms) for faster response, but increases
     CPU usage when idle. Current value is a reasonable trade-off.

7. Single-threaded command execution: all handlers run in the QTimer callback
   - Long operations (execute_processing, render_map) block the timer loop.
   - No concurrent command handling — one command at a time.
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main():
    print("=" * 80)
    print("QGIS MCP Server — Performance Benchmarks")
    print("=" * 80)

    all_results: list[BenchResult] = []

    # Sync benchmarks
    sections = [
        ("JSON Serialization / Deserialization", bench_json_serialization),
        ("Length-Prefix Framing", bench_length_prefix_framing),
        ("getpeername() Syscall Overhead", bench_getpeername_syscall),
        ("get_qgis_connection() Validation", bench_get_qgis_connection),
        ("_send() Helper", bench_send_helper),
    ]

    for title, fn in sections:
        print(f"\n--- {title} ---")
        results = fn()
        for r in results:
            print(r)
        all_results.extend(results)

    # Async benchmarks
    async_sections = [
        ("Tool Invocation (async)", bench_tool_invocation),
        ("Completion Handler", bench_completion_handler),
    ]

    for title, fn in async_sections:
        print(f"\n--- {title} ---")
        results = await fn()
        for r in results:
            print(r)
        all_results.extend(results)

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY: Top 5 slowest operations by median")
    print("=" * 80)
    by_median = sorted(all_results, key=lambda r: r.median_us, reverse=True)
    for r in by_median[:5]:
        print(r)

    # Plugin notes
    print(PLUGIN_NOTES)


if __name__ == "__main__":
    asyncio.run(main())
