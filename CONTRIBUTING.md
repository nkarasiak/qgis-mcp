# Contributing to QGIS MCP

Thank you for your interest in contributing! 🎉
This project connects [QGIS](https://qgis.org/) to [Claude AI](https://claude.ai/chat) through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/docs/getting-started/intro). Your help in improving this integration is very welcome.

## Getting Started

1. **Fork and clone**

   ```bash
   git clone git@github.com:YOUR-USERNAME/qgis-mcp.git
   cd qgis-mcp
   ```

2. **Install prerequisites**

   - QGIS 3.28 or newer (3.28 to 4.x)
   - Python 3.12 or newer
   - [uv](https://docs.astral.sh/uv/getting-started/installation/)

   On macOS:

   ```bash
   brew install uv
   ```

   On Windows PowerShell:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

3. **Run the installer**

   ```bash
   python install.py
   ```

   It symlinks `qgis_mcp_plugin/` into your QGIS profile, sets up the venv, and
   writes the MCP client config for whichever clients you pick from the menu.
   Non interactive variant:

   ```bash
   python install.py --non-interactive --clients claude-desktop,cursor
   ```

   Then restart QGIS, open `Plugins` > `Manage and Install Plugins`, and enable
   **QGIS MCP**.

## Development Workflow

- Start the plugin server (`Plugins` > `QGIS MCP` > `Start Server`).
- Make your changes, then run the tests and the linters:

  ```bash
  uv run --no-sync pytest tests/
  uvx ruff@0.16.0 check qgis_mcp_plugin/ src/ tests/ install.py
  uvx flake8@7.3.0 qgis_mcp_plugin/
  ```

  `tests/test_qgis_live.py` needs a plugin server listening on `localhost:9876`;
  every other test runs against a mocked socket.

## Contributing Guidelines

- Keep PRs focused on a single change.
- Write clear commit messages.
- Update docs if behavior changes.
- Be cautious when using `execute_code` (it runs arbitrary PyQGIS).

## Reporting Issues

- Use [GitHub Issues](https://github.com/nkarasiak/qgis-mcp/issues).
- Include OS, QGIS version, and error logs where relevant.
