# winbox-mcp

An MCP server that lets Claude (or any MCP client) control MikroTik RouterOS routers (v6/v7) — everything WinBox can do.

## Install

```bash
git clone https://github.com/mohammed-w-mosabeh/winbox-mcp.git
claude mcp add -s user winbox -- uv run --directory /path/to/winbox-mcp winbox-mcp
```

## Transports
`api` (8728), `api-ssl` (8729), `rest` / `rest-ssl` (v7 REST on the www port), plus SSH for `ros_cli`,
`ros_export` and SFTP file transfer.

Routers live in `~/.config/winbox-mcp/routers.json` (created by `add_router`, stored with 0600 permissions),
or set `ROS_HOST` / `ROS_USER` / `ROS_PASSWORD` for a single router.

On the router, the `api` service (or `www` for REST) and `ssh` must be enabled: `/ip service print`.

## Tools
`list_routers`, `add_router`, `remove_router`, `discover_neighbors` (MNDP), `ros_print`, `ros_add`, `ros_set`,
`ros_remove`, `ros_enable`, `ros_disable`, `ros_command` (any API command), `ros_cli` (SSH terminal),
`ros_export`, `system_overview`, `ros_ping`, `ros_backup`, `ros_file_download`, `ros_file_upload`.

Menus use API paths such as `/ip/address`, `/interface/bridge/port`, `/routing/ospf/instance`.

See also: [silab-mcp](https://github.com/mohammed-w-mosabeh/silab-mcp) — control SILAB MikroTik labs, and
push lab routers into this server with `silab_routers_to_winbox`.

## License
[MIT](LICENSE)
