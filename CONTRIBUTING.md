

# Contributing to QGIS MCP

Thank you for your interest in contributing! 🎉  
This project connects [QGIS](https://qgis.org/) to [Claude AI](https://claude.ai/chat) through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/docs/getting-started/intro)). Your help in improving this integration is very welcome.

## Getting Started

1. **Fork the Repository**  
   Clone your fork locally:
   ```bash
   git clone git@github.com:YOUR-USERNAME/qgis-mcp-workflows.git
   cd qgis-mcp-workflows
   ```

2. **Install Prerequisites**  
   - QGIS 3.X (tested on 3.22)  
   - Python 3.10 or newer  
   - [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager  
   - Claude desktop  

   On Mac:
   ```bash
   brew install uv
   ```

   On Windows Powershell:
   ```bash
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

3. **Set Up the QGIS Plugin**  
   Create a symlink from this repo’s `qgis_mcp_workflows_plugin` folder to your QGIS profile plugin directory.

   On Mac:
   ```bash
   ln -s $(pwd)/qgis_mcp_workflows_plugin ~/Library/Application\ Support/QGIS/QGIS3/profiles/default/python/plugins/qgis_mcp_workflows_plugin
   ```

   On Windows Powershell:
   ```powershell
   $src = "$(pwd)\qgis_mcp_workflows_plugin"
   $dst = "$env:APPDATA\QGIS\QGIS3\profiles\default\python\plugins\qgis_mcp_workflows_plugin"
   New-Item -ItemType SymbolicLink -Path $dst -Target $src
   ```

   Restart QGIS, go to `Plugins` > `Manage and Install Plugins`, search for **QGIS MCP Workflows**, and enable it.

4. **Configure Claude Desktop**  
   Add the server configuration to `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "qgis-workflows": {
         "command": "uv",
         "args": [
           "--directory",
           "/ABSOLUTE/PATH/TO/qgis-mcp-workflows",
           "run",
           "--no-sync",
           "qgis-mcp-workflows-server"
         ]
       }
     }
   }
   ```

## Development Workflow

- Start the QGIS plugin (`Plugins` > `QGIS MCP Workflows` > `Start Server`).
- Run the MCP server via Claude Desktop integration.
- Make your changes and test locally.

## Contributing Guidelines

- Keep PRs focused on a single change.
- Write clear commit messages.
- Update docs if behavior changes.
- Be cautious when using `execute_code` (it runs arbitrary PyQGIS).

## Reporting Issues

- Use [GitHub Issues](https://github.com/wattwong103/qgis-mcp-workflows/issues).
- Include OS, QGIS version, and error logs where relevant.