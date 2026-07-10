# Using QGIS MCP with Agent Clients

QGIS MCP uses the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) over **stdio**
transport, which makes it compatible with any MCP-capable agent framework — not just Claude.
This guide covers how to register the server in various agent environments, including
Nous/Hermes-style coding agents that support MCP tool-use.

## How it works

```
Agent (LLM) ←→ MCP Client ←→ stdio ←→ qgis-mcp-server ←→ TCP socket ←→ QGIS Plugin
```

The MCP server is a plain Python process launched by the agent's MCP client.  No special
Nous-specific API is involved — any agent that can spawn an MCP stdio server and call tools
can use QGIS MCP.

---

## Prerequisites

1. **QGIS 3.28+** installed with the **QGIS MCP plugin** enabled:
   - `Plugins` › `Manage and Install Plugins` › search **QGIS MCP** › Install.
   - Restart QGIS, then open the QGIS MCP dock widget and click **Start Server**.
   - The plugin listens on `localhost:9876` by default.

2. **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — the Python package
   runner used to launch the MCP server without a manual `pip install`.

---

## MCP client configuration (generic)

Any MCP-capable client that accepts a JSON or TOML config block can use this snippet:

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uvx",
      "args": [
        "--from",
        "https://github.com/GetBack2Basics/qgis-mcp/archive/refs/heads/main.zip",
        "qgis-mcp-server"
      ]
    }
  }
}
```

Or, if you have a local clone of this repository:

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uv",
      "args": ["--directory", "/path/to/qgis-mcp", "run", "--no-sync", "src/qgis_mcp/server.py"]
    }
  }
}
```

### With optional environment variables

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uvx",
      "args": [
        "--from",
        "https://github.com/GetBack2Basics/qgis-mcp/archive/refs/heads/main.zip",
        "qgis-mcp-server"
      ],
      "env": {
        "QGIS_MCP_HOST": "localhost",
        "QGIS_MCP_PORT": "9876",
        "QGIS_MCP_TOKEN": "your-long-random-secret"
      }
    }
  }
}
```

---

## Nous / Hermes-style agents

The [Nous Research portal](https://portal.nousresearch.com/) and Hermes-based agents that
support MCP tool-use can connect to QGIS MCP just like any other MCP client.

### Step 1 — Identify how your agent loads MCP servers

Nous/Hermes agents that support the MCP tool-calling interface typically accept a server
configuration in one of these forms:

- A JSON config file (often `mcp.json`, `mcp_servers.json`, or `opencode.json`).
- A CLI flag such as `--mcp-server "command args"`.
- An environment variable or API payload listing server configs.

Consult your specific agent client's documentation for the exact format, then paste in the
generic JSON block above.

### Step 2 — Verify tool discovery

Once the agent starts the MCP server, it should list the available tools.  You can verify
by asking the agent:

```
List the available MCP tools for QGIS.
```

You should see 103 tools (e.g. `ping`, `get_layers`, `render_map`, …).

### Step 3 — Check the connection

Ask the agent to call the `ping` tool:

```
Call the QGIS ping tool to verify the connection.
```

If QGIS is running with the plugin started you will get `{"pong": true}`.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `QGIS_MCP_HOST` | `localhost` | Host where the QGIS plugin socket listens |
| `QGIS_MCP_PORT` | `9876` | Port for the QGIS plugin socket |
| `QGIS_MCP_TOKEN` | _(unset)_ | Optional shared secret for socket auth |
| `QGIS_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` or `streamable-http` |
| `QGIS_MCP_LOG_FILE` | `~/.local/share/qgis-mcp/server.log` | Log file path (empty to disable) |
| `QGIS_MCP_LOG_LEVEL` | `INFO` | File log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `QGIS_MCP_TOOL_MODE` | `granular` | `granular` (103 tools) or `compound` (~23 grouped tools) |

---

## Troubleshooting

### "No response from QGIS" / connection refused

- Confirm QGIS is open and the QGIS MCP dock widget shows **Server running**.
- Check the port: the plugin defaults to `9876`.  Override with `QGIS_MCP_PORT` if you
  changed it in the plugin settings.
- On macOS/Linux run `ss -tlnp | grep 9876` (or `netstat -an | grep 9876`) to confirm the
  port is listening.

### Tools not appearing / empty tool list

- Make sure the MCP server process started successfully.  Run it manually to check:
  ```bash
  uvx --from https://github.com/GetBack2Basics/qgis-mcp/archive/refs/heads/main.zip qgis-mcp-server
  ```
  It should print nothing and wait (reading MCP messages from stdin). `Ctrl-C` to quit.
- If you see `ModuleNotFoundError`, `uv`/`uvx` is not installed or not on `PATH`.
- If you see `Connection refused`, the QGIS plugin is not listening yet — start it first.

### Agent says "tool call failed" for every command

- The MCP server started but can't reach QGIS.  Confirm the plugin is running (see above).
- If you set `QGIS_MCP_TOKEN`, make sure the **same** token is in the MCP server's `env`
  block in your config.

### Streamable-HTTP transport (remote / multi-client)

If the agent cannot spawn subprocesses but can make HTTP requests, set
`QGIS_MCP_TRANSPORT=streamable-http` in the server environment and point the client at
`http://localhost:8000/mcp/` (default FastMCP HTTP endpoint).

---

## Compound tool mode

If your agent has a limited context window or struggles with 103 separate tool schemas,
enable compound tool mode to reduce the tool count to 23 grouped tools:

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uvx",
      "args": ["--from", "...", "qgis-mcp-server"],
      "env": { "QGIS_MCP_TOOL_MODE": "compound" }
    }
  }
}
```

Each compound tool takes an `action` parameter to select the operation.  The groups are:
`system`, `project`, `layer`, `features`, `selection`, `style`, `canvas`, `render`,
`processing`, `code`, `batch`, `layer_tree`, `plugins`, `variables`, `settings`,
`expression`, `transform`, `message_log`, `layer_property`.
