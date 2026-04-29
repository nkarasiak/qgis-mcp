# QGIS MCP

Connect [QGIS](https://qgis.org/) to [Claude AI](https://claude.ai/) through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), enabling Claude to directly control QGIS — manage layers, edit features, run processing algorithms, render maps, and more.

51 MCP tools covering layer management, feature editing, processing, rendering, styling, plugin development, and system management. Compatible with QGIS 3.28–4.x. Includes a one-command installer for 6+ MCP clients.

## Architecture

```
Claude ←→ MCP Server (FastMCP) ←→ TCP socket ←→ QGIS Plugin (QTimer) ←→ PyQGIS API
```

1. **QGIS Plugin** (`qgis_mcp_plugin/`) — Runs inside QGIS. Non-blocking TCP socket server that processes JSON commands within QGIS's event loop.
2. **MCP Server** (`src/qgis_mcp/server.py`) — Runs outside QGIS. Exposes QGIS operations as MCP tools via [FastMCP](https://gofastmcp.com/).

## Prerequisites

- **QGIS** 3.28 or newer
- **Python** 3.12+
- **uv** package manager — [install uv](https://docs.astral.sh/uv/getting-started/installation/)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/nkarasiak/qgis-mcp.git
cd qgis-mcp
```

### 2. Install with the one-command installer (recommended)

```bash
python install.py
```

This will:
- Symlink the QGIS plugin into your QGIS profile
- Configure your MCP client(s) — supports Claude Desktop, Claude Code, Cursor, VS Code Copilot, Windsurf, and Zed

Options: `--non-interactive --clients claude-desktop,cursor` for CI, `--remote` for uvx-based installs, `--profile myprofile` for non-default QGIS profiles, `--uninstall` to remove.

Restart QGIS, then enable the plugin: `Plugins` > `Manage and Install Plugins` > search "QGIS MCP" > check the box.

<details>
<summary><b>Manual installation</b></summary>

#### Install the QGIS plugin manually

Copy (or symlink) the `qgis_mcp_plugin/` folder into your QGIS plugins directory:

**Find your plugins folder:** In QGIS, go to `Settings` > `User Profiles` > `Open Active Profile Folder`, then navigate to `python/plugins/`.

| OS | Typical path |
|----|-------------|
| Linux | `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/` |
| macOS | `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/` |
| Windows | `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\` |

```bash
# Example on Linux (symlink recommended for development)
ln -s /path/to/qgis-mcp/qgis_mcp_plugin ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_plugin
```

Restart QGIS, then enable the plugin: `Plugins` > `Manage and Install Plugins` > search "QGIS MCP" > check the box.

#### Connect your MCP client manually

##### Claude Code — project-level config (recommended)

Create a `.mcp.json` file at the root of your clone:

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uv",
      "args": ["run", "src/qgis_mcp/server.py"],
      "cwd": "/path/to/qgis-mcp"
    }
  }
}
```

Claude Code automatically detects `.mcp.json` when you open the project — no manual `claude mcp add` needed.

##### Claude Code — one-liner (remote install)

```bash
claude mcp add --transport stdio qgis-mcp -- uvx --from git+https://github.com/nkarasiak/qgis-mcp qgis-mcp-server
```

##### Claude Desktop

Go to `Claude` > `Settings` > `Developer` > `Edit Config` and add:

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/qgis-mcp",
        "run", "src/qgis_mcp/server.py"
      ]
    }
  }
}
```

> **Microsoft Store (MSIX) Claude Desktop on Windows:** the `--directory` form
> shown above is required — `cwd` will not work. Store-installed Claude Desktop
> runs MCP servers inside an MSIX package sandbox that silently drops the `cwd`
> field, so configs using `cwd` fail with
> `Failed to spawn: src/qgis_mcp/server.py — The system cannot find the path specified`.
> Standalone (`.exe` from claude.ai) installs honor `cwd` fine; the
> `--directory` form works in both, so it's the safer default. You can spot a
> Store install when the config file lives under
> `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\` instead of
> the usual `%APPDATA%\Claude\`.

Or for a remote install without cloning:

```json
{
  "mcpServers": {
    "qgis": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/nkarasiak/qgis-mcp",
        "qgis-mcp-server"
      ]
    }
  }
}
```

##### Cursor / other MCP clients

Use the same JSON configuration above in your client's MCP settings file.

</details>

## Usage

1. **Start the plugin** — In QGIS, click the MCP toolbar button (or `Plugins` > `QGIS MCP`) and click "Start Server"
2. **Talk to Claude** — The MCP tools will appear automatically. Ask Claude to work with your QGIS project.

### Example prompt

```
You have access to QGIS tools. Do the following:
1. Ping to check the connection
2. Create a new project and save it at "/tmp/my_project.qgz"
3. Load the vector layer "/data/cities.shp" and name it "Cities"
4. Get field statistics for the "population" field
5. Create a graduated symbology on the "population" field with 5 classes
6. Render the map and show me the result
7. Save the project
```

## Tools (51)

| Category | Tools |
|----------|-------|
| **Project** | `load_project`, `create_new_project`, `save_project`, `get_project_info` |
| **Layers** | `get_layers`, `add_vector_layer`, `add_raster_layer`, `remove_layer`, `find_layer`, `create_memory_layer`, `set_layer_visibility`, `zoom_to_layer`, `get_layer_extent`, `set_layer_property` |
| **Features** | `get_layer_features`, `add_features`, `update_features`, `delete_features`, `select_features`, `get_selection`, `clear_selection`, `get_field_statistics` |
| **Styling** | `set_layer_style` (single, categorized, graduated) |
| **Rendering** | `render_map`, `get_canvas_screenshot`, `get_canvas_extent`, `set_canvas_extent` |
| **Processing** | `execute_processing`, `list_processing_algorithms`, `get_algorithm_help` |
| **Layouts** | `list_layouts`, `export_layout` |
| **Layer tree** | `get_layer_tree`, `create_layer_group`, `move_layer_to_group` |
| **Plugins** | `list_plugins`, `get_plugin_info`, `reload_plugin` |
| **System** | `ping`, `diagnose`, `get_qgis_info`, `get_raster_info`, `get_message_log`, `execute_code`, `batch_commands`, `validate_expression`, `get_project_variables`, `set_project_variable`, `get_setting`, `set_setting`, `transform_coordinates` |

All tools are async with human-readable titles and annotations (`readOnly`, `destructive`, `idempotent`). Destructive tools ask for confirmation via MCP elicitation when supported; clients without elicitation proceed normally (fail-open) since tools are already gated by `ToolAnnotations`. Long-running tools report progress via MCP logging.

### Compound tool mode

Set `QGIS_MCP_TOOL_MODE=compound` to reduce the 51 granular tools to ~19 grouped tools, cutting schema overhead per LLM turn. Each compound tool takes an `action` parameter:

```bash
QGIS_MCP_TOOL_MODE=compound uv run --no-sync src/qgis_mcp/server.py
```

Groups: `system`, `project`, `layer`, `features`, `selection`, `style`, `canvas`, `render`, `processing`, `code`, `batch`, `layer_tree`, `plugins`, `variables`, `settings`, `expression`, `transform`, `message_log`, `layer_property`.

## Configuration

| Environment variable | Default | Description |
|---------------------|---------|-------------|
| `QGIS_MCP_HOST` | `localhost` | Host for socket connection |
| `QGIS_MCP_PORT` | `9876` | Port for socket connection |
| `QGIS_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` or `streamable-http` |
| `QGIS_MCP_LOG_FILE` | `~/.local/share/qgis-mcp/server.log` | Log file path (empty to disable) |
| `QGIS_MCP_LOG_LEVEL` | `INFO` | File log level |
| `QGIS_MCP_TOOL_MODE` | `granular` | `granular` (51 tools) or `compound` (~19 grouped) |

## Development

```bash
# Run unit tests (no QGIS needed — mocked socket)
uv run --no-sync pytest tests/test_mcp_tools.py -v

# Run integration tests (requires QGIS plugin running)
uv run --no-sync pytest tests/test_qgis_live.py -v
```

## License

This project is licensed under the GNU GPL v2 or later.
