---
name: qgis-mcp-tools
description: Reference for all qgis-mcp MCP tools, resources, and prompts (names, titles, annotations, descriptions). Use when adding/modifying an MCP tool, explaining what a tool does, or checking tool annotations (readOnly/destructive/idempotent).
---

# MCP Tools (118 total)

| Tool | Title | Annotations | Description |
|---|---|---|---|
| `ping` | Ping | readOnly | Check server connectivity |
| `diagnose` | Diagnose | readOnly | Full stack health check: QGIS version, plugin/server version match, providers, clients |
| `list_qgis_instances` | List QGIS Instances | readOnly | Configured QGIS instances (name, host, port) + current reachability |
| `get_qgis_info` | Get QGIS Info | readOnly | QGIS version, profile, plugins |
| `get_project_info` | Get Project Info | readOnly | Project metadata, CRS, layers |
| `load_project` | Load Project | - | Load a .qgs/.qgz file |
| `create_new_project` | Create New Project | - | Create and save new project |
| `save_project` | Save Project | idempotent | Save project to current or new path |
| `get_layers` | Get Layers | readOnly | List layers with pagination (limit/offset) |
| `add_vector_layer` | Add Vector Layer | - | Add vector layer (shapefile, GeoJSON, etc.) |
| `add_raster_layer` | Add Raster Layer | - | Add raster layer (GeoTIFF, etc.) |
| `remove_layer` | Remove Layer | destructive | Remove layer by ID (elicitation) |
| `find_layer` | Find Layer | readOnly | Find layers by name pattern (fnmatch/substring) |
| `create_memory_layer` | Create Memory Layer | - | Create in-memory vector layer with fields |
| `set_layer_visibility` | Set Layer Visibility | idempotent | Show/hide layer in layer tree |
| `zoom_to_layer` | Zoom to Layer | idempotent | Zoom canvas to layer extent |
| `get_layer_features` | Get Layer Features | readOnly | Flat features with _fid, expression filter, limit/offset, geometry |
| `get_field_statistics` | Get Field Statistics | readOnly | Aggregate stats for a field (count, mean, min, max, etc.) |
| `add_features` | Add Features | destructive | Add features to a vector layer |
| `update_features` | Update Features | destructive | Update feature attributes by fid |
| `delete_features` | Delete Features | destructive | Delete features by fids or expression (elicitation) |
| `select_features` | Select Features | idempotent | Select features by expression or fids |
| `get_selection` | Get Selection | readOnly | Get selected feature IDs and count |
| `clear_selection` | Clear Selection | idempotent | Clear layer selection |
| `set_layer_style` | Set Layer Style | - | Apply single/categorized/graduated symbology |
| `get_canvas_extent` | Get Canvas Extent | readOnly | Current map canvas extent and CRS |
| `set_canvas_extent` | Set Canvas Extent | idempotent | Set canvas extent with optional CRS transform |
| `get_canvas_screenshot` | Get Canvas Screenshot | readOnly | Fast canvas widget grab (no re-render), inline image |
| `get_raster_info` | Get Raster Info | readOnly | Raster band count, stats, nodata, dimensions |
| `execute_processing` | Execute Processing | - | Run QGIS Processing algorithm (plugin cancels after `timeout` seconds, default 55; socket outlasts it, async+progress+logging) |
| `list_processing_algorithms` | List Processing Algorithms | readOnly | Search algorithms by keyword/provider |
| `get_algorithm_help` | Get Algorithm Help | readOnly | Algorithm parameters, outputs, description |
| `create_processing_model` | Create Processing Model | - | Build a `.model3` workflow from a structured spec (inputs, steps, outputs); always saved into the QGIS user models folder and registered (numeric suffix on name collision); supports `@input` / `$step.OUTPUT` / `=expression` references |
| `render_map` | Render Map | idempotent | Render canvas to inline image (60s, async+progress+logging) |
| `execute_code` | Execute Code | destructive | Run arbitrary PyQGIS code (plugin cancels past `timeout` seconds, default 55, returning `timed_out` and the output so far; result carries `elapsed`; async+progress+logging) |
| `batch_commands` | Batch Commands | - | Multiple commands in one round-trip |
| `list_layouts` | List Layouts | readOnly | List print layouts |
| `export_layout` | Export Layout | idempotent | Export print layout to PDF/PNG/SVG |
| `get_message_log` | Get Message Log | readOnly | Get QGIS message log entries, filter by level/tag |
| `list_plugins` | List Plugins | readOnly | List installed plugins with enabled status |
| `get_plugin_info` | Get Plugin Info | readOnly | Detailed plugin info (version, author, path) |
| `reload_plugin` | Reload Plugin | destructive | Reload a plugin (blocks self-reload, logging) |
| `get_layer_tree` | Get Layer Tree | readOnly | Recursive layer tree with groups and layers |
| `create_layer_group` | Create Layer Group | - | Create a group in the layer tree |
| `move_layer_to_group` | Move Layer to Group | - | Move a layer into a group |
| `set_layer_property` | Set Layer Property | idempotent | Set opacity, name, scale visibility, min/max scale |
| `get_layer_extent` | Get Layer Extent | readOnly | Layer bounding box and CRS |
| `get_project_variables` | Get Project Variables | readOnly | Project-level variables (key-value) |
| `set_project_variable` | Set Project Variable | idempotent | Set a project variable (@key in expressions) |
| `validate_expression` | Validate Expression | readOnly | Validate QGIS expression, get referenced columns |
| `get_setting` | Get Setting | readOnly | Read a QGIS setting by key path |
| `set_setting` | Set Setting | destructive | Write a QGIS setting (elicitation) |
| `transform_coordinates` | Transform Coordinates | readOnly | CRS transform for points, point lists, or bboxes |
| `list_processing_models` | List Processing Models | readOnly | List registered Processing models (id, name, group) |
| `run_model` | Run Model | - | Run a model by registered id or .model3 path (60s, async+progress) |
| `get_processing_providers` | Get Processing Providers | readOnly | List providers (native/gdal/grass/...) with algo counts + active status |
| `execute_processing_batch` | Execute Processing Batch | - | Run one algorithm over many parameter dicts; per-run success/error/skipped status (`timeout` bounds the whole batch, default 55s) |
| `raster_calculator` | Raster Calculator | - | Band math via QgsRasterCalculator, 'Name@band' refs, GeoTIFF out (60s) |
| `zonal_statistics` | Zonal Statistics | - | Per-polygon raster stats (native:zonalstatisticsfb), memory or file out (60s) |
| `sample_raster_values` | Sample Raster Values | readOnly | Sample pixel values at [x,y] points (raster CRS), one/all bands |
| `export_layer` | Export Layer | idempotent | Export vector/raster to disk; target_crs reproject, filter_expression subset (60s) |
| `field_calculator` | Field Calculator | - | Add+populate field from QGIS expression, in-place |
| `get_unique_values` | Get Unique Values | readOnly | Distinct values of a field (limit, -1 for all) |
| `spatial_join` | Spatial Join | - | Join attributes by location (native:joinattributesbylocation), memory or file out (60s) |
| `get_layout_info` | Get Layout Info | readOnly | List items in a print layout (type, id, uuid, position, size) |
| `add_layout_label` | Add Layout Label | - | Add a text label (supports `[% expr %]`) to a layout |
| `add_layout_legend` | Add Layout Legend | - | Add a legend linked to a map item |
| `add_layout_scalebar` | Add Layout Scale Bar | - | Add a scale bar linked to a map item |
| `add_layout_picture` | Add Layout Picture | - | Add a picture/SVG (logo, north arrow) to a layout |
| `add_layout_table` | Add Layout Table | - | Add an attribute table for a vector layer to a layout |
| `configure_atlas` | Configure Atlas | - | Configure a layout atlas (coverage layer, page name/filter/sort) |
| `export_atlas` | Export Atlas | idempotent | Export atlas: single multi-page PDF, or one image per feature (60s) |
| `remove_layout` | Remove Layout | destructive | Remove a print layout (elicitation) |
| `execute_sql` | Execute SQL | - | SQL across loaded layers via virtual layer; rows inline or as a new layer (60s) |
| `evaluate_expression` | Evaluate Expression | readOnly | Evaluate a standalone QGIS expression to a scalar (aggregate, @vars) |
| `identify_features` | Identify Features | readOnly | Features at a point [x,y] across layers (map-click analogue) |
| `duplicate_layer` | Duplicate Layer | - | Duplicate a layer (with style) under a new name |
| `set_layer_order` | Set Layer Order | idempotent | Set explicit layer draw order (top to bottom) |
| `set_raster_style` | Set Raster Style | - | Raster symbology: `singleband_pseudocolor` (ramp + classification/interpolation), `singleband_gray`, `multiband_color` (RGB), `hillshade`. min/max default to band statistics |
| `update_feature_geometry` | Update Feature Geometry | destructive | Replace geometries by fid from WKT (layer CRS); undoable inside an edit session |
| `start_editing` | Start Editing | - | Open an edit session; feature writes then go to the undoable buffer |
| `commit_edits` | Commit Edits | - | Write the edit buffer to the data source and close the session |
| `rollback_edits` | Rollback Edits | destructive | Discard every uncommitted change (elicitation) |
| `get_edit_status` | Get Edit Status | readOnly | Editable/modified flags, undo-redo availability, pending add/delete/change counts |
| `undo_edits` | Undo Edits | - | Undo N steps on the layer's own undo stack |
| `redo_edits` | Redo Edits | - | Redo N previously undone steps |
| `list_connections` | List Connections | readOnly | Saved Browser-panel connections (PostgreSQL, GeoPackage, SpatiaLite, ...); passwords redacted |
| `create_postgresql_connection` | Create PostgreSQL Connection | - | Validate and save a password-free PostgreSQL Browser-panel connection. `connection_mode` selects the required parameters: `endpoint_using_auth_manager` (host, port, database, auth_config_id; the caller supplies the real port, never an assumed `5432`), `service_using_auth_manager` (service, auth_config_id), `service_only` (service). Service modes take `database` as an optional dbname override; parameters a mode does not use are rejected |
| `list_connection_tables` | List Connection Tables | readOnly | Schemas, then tables with geometry column, CRS, primary key and kind |
| `add_layer_from_connection` | Add Layer from Connection | - | Load a connection table, or a database-side SQL query, as a layer (60s) |
| `import_layer_to_connection` | Import Layer to Connection | destructive | Write a vector layer into a connection as a new table; `overwrite` elicits (60s) |
| `execute_connection_sql` | Execute Connection SQL | destructive | Server-side SQL on the connection's database (elicitation, 60s) - not the virtual-layer `execute_sql` |
| `get_active_layer` | Get Active Layer | readOnly | Currently active (selected) layer in the QGIS layer panel |
| `set_active_layer` | Set Active Layer | idempotent | Set the active layer in the layer panel by layer ID |
| `get_canvas_scale` | Get Canvas Scale | readOnly | Canvas scale, rotation and magnification factor |
| `set_canvas_scale` | Set Canvas Scale | idempotent | Set canvas scale (denominator, e.g. 50000) and/or rotation (0–360°) |
| `get_3d_screenshot` | Get 3D Screenshot | readOnly | Capture an **open** 3D Map View as an inline image (reuses its scene + camera via a layout 3D map item; optional pitch/heading/distance overrides). `get_canvas_screenshot` cannot grab the OpenGL view |
| `get_layer_labeling` | Get Layer Labeling | readOnly | Labeling config of a vector layer: enabled, field, font size, color |
| `set_layer_labeling` | Set Layer Labeling | - | Configure labels (`enabled`, `field_name`, `font_size`, `color`) |
| `apply_style_qml` | Apply Style QML | - | Apply a QML style file to a layer |
| `save_style_qml` | Save Style QML | - | Save a layer's style to a QML file |
| `get_layer_crs` | Get Layer CRS | readOnly | Layer CRS: EPSG code, description, geographic flag, PROJ4 string |
| `set_layer_crs` | Set Layer CRS | - | Set a layer's CRS - reinterprets coordinates, does **not** reproject data |
| `set_project_crs` | Set Project CRS | - | Set the project CRS (changes how layers project onto the canvas) |
| `get_bookmarks` | Get Bookmarks | readOnly | Spatial bookmarks (name, group, extent, CRS) |
| `add_bookmark` | Add Bookmark | - | Add a spatial bookmark from a name + extent and CRS |
| `remove_bookmark` | Remove Bookmark | destructive | Remove a spatial bookmark by ID |
| `get_map_themes` | Get Map Themes | readOnly | Map themes (visibility presets) and the layers each shows |
| `add_map_theme` | Add Map Theme | - | Save current layer visibility as a theme (updates an existing name) |
| `apply_map_theme` | Apply Map Theme | idempotent | Restore the layer visibility state saved in a theme |
| `remove_map_theme` | Remove Map Theme | destructive | Remove a map theme by name |
| `add_web_layer` | Add Web Layer | - | Add an XYZ / WMS / WFS layer to the project |
| `add_table_join` | Add Table Join | - | Join a second layer's attributes onto a vector layer |
| `add_field` | Add Field | - | Add a field (`string`, `int`, `double`, `bool`, `date`, `datetime`) |
| `rename_field` | Rename Field | - | Rename a field on a vector layer |
| `delete_field` | Delete Field | destructive | Delete a field from a vector layer |
| `create_layout` | Create Layout | - | Create a new print layout |
| `add_layout_map` | Add Layout Map | - | Add a map item to a layout at a position and size (mm) |

## MCP Resources

| URI | Description |
|---|---|
| `qgis://info` | QGIS version, profile, plugin count |
| `qgis://project` | Current project metadata |
| `qgis://layers` | All layers summary |
| `qgis://layers/{layer_id}/info` | Detailed layer info (CRS, extent, fields, source) |
| `qgis://layers/{layer_id}/features` | Sample features (first 10) |
| `qgis://layers/{layer_id}/schema` | Field names, types, lengths |
| `qgis://llms.txt` | LLM context: tool categories, usage patterns, quick start guide |
| `qgis://cache/{cache_id}` | A large tool result parked in the server-side cache (`_resource_cache`) instead of inlined |

## MCP Prompts

| Prompt | Description |
|---|---|
| `analyze_layer` | Inspect schema, sample data, compute statistics |
| `spatial_analysis` | Spatial operation between two layers with CRS check |
| `style_map` | Create thematic map with symbology (now uses set_layer_style tool) |
| `create_processing_model` | Translate a natural-language workflow description into a saved `.model3` Processing Model |

## MCP Protocol Features

- **MCP Logging**: Long-running tools (`execute_processing`, `render_map`, `execute_code`) and notable operations (`load_project`, `reload_plugin`) send `ctx.info()` status messages to the client.
- **Elicitation**: Destructive tools (`remove_layer`, `delete_features`, `set_setting`, `execute_code`, `rollback_edits`, `execute_connection_sql`, `import_layer_to_connection` when `overwrite`) ask for user confirmation via `ctx.elicit()`. Commands whose tool always elicits are in `BATCH_BLOCKED_COMMANDS` so `batch` cannot bypass the prompt. Fail-open: proceeds if the client doesn't support elicitation (tools are already gated by `ToolAnnotations(destructiveHint=True)`).
- **Completions**: `layer_id` arguments support auto-completion from available layers.
- **Tool Titles**: All 118 tools have human-readable `title=` for better display in Claude Desktop / Cursor.
- **Tool Annotations**: `readOnly`, `destructive`, `idempotent` hints via `ToolAnnotations`.
- **Streamable HTTP**: Set `QGIS_MCP_TRANSPORT=streamable-http` for remote/multi-client support.
- **Compound Tool Mode**: Set `QGIS_MCP_TOOL_MODE=compound` to replace the granular tools with 27 grouped tools (full parity: every granular command is reachable via an action), reducing schema overhead per LLM turn. Each compound tool takes `action` (str, required) plus `params` (object, optional) holding that action's parameters - e.g. `{"action": "load", "params": {"path": "/data/x.qgz"}}`. Handlers must never use `**kwargs`: FastMCP/pydantic cannot express variadic kwargs in JSON Schema and degrades them to a single required string (issue #24).
- **Structured File Logging**: Rotating file log (5MB x 3 backups) at `~/.local/share/qgis-mcp/server.log`. Console (stderr) only shows WARNING+. Configure via `QGIS_MCP_LOG_FILE` and `QGIS_MCP_LOG_LEVEL`.
