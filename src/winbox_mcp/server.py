"""WinBox MCP: control MikroTik RouterOS devices (everything WinBox can do) from Claude.

Transports per router:
  api / api-ssl   RouterOS API (8728 / 8729)          -> full config read/write, v6 + v7
  rest / rest-ssl RouterOS v7 REST via www / www-ssl  -> same, when only the web port is reachable
  ssh             raw CLI, /export, SFTP file transfer (used by ros_cli / ros_export / files)
Plus MNDP neighbour discovery (WinBox's "Neighbors" tab).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import mndp
from .rest_api import RouterOSRest
from .routeros_api import RouterOSApi, RouterOSError

mcp = FastMCP(
    "winbox",
    instructions=(
        "Control MikroTik RouterOS routers like WinBox does. Start with list_routers (or discover_neighbors "
        "then add_router). Menus use API paths: '/ip/address', '/interface/bridge/port', '/routing/ospf/instance'. "
        "Read with ros_print, change with ros_add / ros_set / ros_remove / ros_enable / ros_disable, run any "
        "API command with ros_command (e.g. '/system/reboot', '/tool/ping'), and use ros_cli for plain terminal "
        "commands (SSH) such as '/export' or scripts. Items can be referenced by .id ('*1') or by name."
    ),
)

CONFIG_PATH = Path(os.environ.get("WINBOX_MCP_CONFIG", Path.home() / ".config" / "winbox-mcp" / "routers.json"))
_lock = threading.RLock()
_routers: dict[str, dict[str, Any]] = {}
_clients: dict[str, Any] = {}
_default: str | None = None


# ------------------------------------------------------------------ config
def _load_config() -> None:
    global _default
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text())
        _routers.update(data.get("routers", {}))
        _default = data.get("default")
    host = os.environ.get("ROS_HOST")
    if host:
        _routers.setdefault("default", {
            "host": host,
            "username": os.environ.get("ROS_USER", "admin"),
            "password": os.environ.get("ROS_PASSWORD", ""),
            "transport": os.environ.get("ROS_TRANSPORT", "api"),
            "port": int(os.environ["ROS_PORT"]) if os.environ.get("ROS_PORT") else None,
            "ssh_port": int(os.environ.get("ROS_SSH_PORT", "22")),
        })
        _default = _default or "default"
    if not _default and _routers:
        _default = next(iter(_routers))


def _save_config() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"default": _default, "routers": _routers}, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def _cfg(router: str | None) -> tuple[str, dict[str, Any]]:
    if (router and router not in _routers) or not _routers:
        _load_config()  # the file may have been updated (e.g. by silab_routers_to_winbox)
    name = router or _default
    if not name:
        raise ValueError("No router configured. Use add_router first (or discover_neighbors to find some).")
    if name not in _routers:
        raise ValueError(f"Unknown router '{name}'. Known: {', '.join(_routers) or 'none'}")
    return name, _routers[name]


def _client(router: str | None):
    name, c = _cfg(router)
    with _lock:
        if name not in _clients:
            t = c.get("transport", "api")
            kw = dict(host=c["host"], username=c.get("username", "admin"), password=c.get("password", ""),
                      port=c.get("port"), use_ssl=t.endswith("ssl"), verify_ssl=c.get("verify_ssl", False))
            _clients[name] = RouterOSRest(**kw) if t.startswith("rest") else RouterOSApi(**kw)
        return _clients[name]


def _run(router, path, params=None, queries=None, proplist=None):
    try:
        return _client(router).command(path, params, queries, proplist)
    except RouterOSError as e:
        raise RuntimeError(str(e)) from e


def _menu(path: str) -> str:
    p = "/" + path.strip().strip("/").replace(" ", "/")
    return p


def _resolve_id(router, menu: str, item: str) -> str:
    """Accept '*1A', a name, or 'key=value' and return the .id."""
    if item.startswith("*"):
        return item
    if "=" in item:
        k, v = item.split("=", 1)
        q = [f"?{k}={v}"]
    else:
        q = [f"?name={item}"]
    rows = _run(router, menu + "/print", queries=q, proplist=[".id"])
    ids = [r.get(".id") for r in rows if r.get(".id")]
    if not ids and "=" not in item:  # some menus key on other fields
        for key in ("default-name", "comment", "address", "user", "dst-address"):
            rows = _run(router, menu + "/print", queries=[f"?{key}={item}"], proplist=[".id"])
            ids = [r.get(".id") for r in rows if r.get(".id")]
            if ids:
                break
    if not ids:
        raise RuntimeError(f"No item matching '{item}' in {menu}")
    if len(ids) > 1:
        raise RuntimeError(f"'{item}' matches {len(ids)} items in {menu} ({', '.join(ids)}); pass an .id")
    return ids[0]


def _where_to_queries(where: dict[str, Any] | None, query: list[str] | None) -> list[str]:
    q = [f"?{k}={'yes' if v is True else 'no' if v is False else v}" for k, v in (where or {}).items()]
    return q + list(query or [])


# ------------------------------------------------------------------ SSH
def _ssh(router: str | None):
    import paramiko
    _, c = _cfg(router)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(c["host"], port=int(c.get("ssh_port") or 22), username=c.get("username", "admin"),
                password=c.get("password", ""), look_for_keys=False, allow_agent=False, timeout=15,
                banner_timeout=20, auth_timeout=20)
    return cli


def _ssh_exec(router: str | None, command: str, timeout: float = 120) -> str:
    cli = _ssh(router)
    try:
        _, out, err = cli.exec_command(command, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        return (o + (("\n[stderr]\n" + e) if e.strip() else "")).replace("\r\n", "\n").strip()
    finally:
        cli.close()


# ================================================================== tools: routers
@mcp.tool()
def list_routers() -> dict:
    """List the routers this server knows (passwords hidden) and which one is the default."""
    return {"default": _default, "config_file": str(CONFIG_PATH), "routers": {
        n: {k: v for k, v in c.items() if k != "password"} for n, c in _routers.items()}}


@mcp.tool()
def add_router(name: str, host: str, username: str = "admin", password: str = "", transport: str = "api",
               port: int | None = None, ssh_port: int = 22, make_default: bool = False, save: bool = True) -> dict:
    """Add (or replace) a router.

    transport: 'api' (8728), 'api-ssl' (8729), 'rest' (v7 REST over http/www, e.g. a WebFig port) or 'rest-ssl'.
    port: the API/REST port if not standard. ssh_port: used by ros_cli / ros_export / file tools.
    save: persist to the config file (password stored there with 0600 permissions).
    """
    global _default
    if transport not in ("api", "api-ssl", "rest", "rest-ssl"):
        raise ValueError("transport must be api, api-ssl, rest or rest-ssl")
    with _lock:
        _routers[name] = {"host": host, "username": username, "password": password, "transport": transport,
                          "port": port, "ssh_port": ssh_port}
        old = _clients.pop(name, None)
        if old:
            old.close()
        if make_default or not _default:
            _default = name
        if save:
            _save_config()
    ident = _run(name, "/system/identity/print")
    return {"added": name, "default": _default, "identity": ident[0].get("name") if ident else None}


@mcp.tool()
def remove_router(name: str) -> str:
    """Forget a router (does not touch the device)."""
    global _default
    with _lock:
        _routers.pop(name, None)
        c = _clients.pop(name, None)
        if c:
            c.close()
        if _default == name:
            _default = next(iter(_routers), None)
        _save_config()
    return f"removed {name}"


@mcp.tool()
def discover_neighbors(seconds: float = 6.0) -> list[dict]:
    """Find MikroTik devices on the local networks via MNDP (like WinBox's Neighbors tab)."""
    return mndp.discover(seconds)


# ================================================================== tools: config
@mcp.tool()
def ros_print(path: str, router: str | None = None, where: dict[str, Any] | None = None,
              proplist: list[str] | None = None, query: list[str] | None = None,
              detail: bool = False) -> list[dict]:
    """Read a menu (like opening a WinBox window). path e.g. '/ip/address', '/interface', '/ip/firewall/filter',
    '/routing/ospf/neighbor', '/system/resource', '/log'.
    where: equality filters {'interface': 'ether1', 'disabled': False}. proplist: only these fields.
    query: raw API query words, e.g. ['?>bytes=1000', '?#|'].
    """
    params = {"detail": ""} if detail else None
    return _run(router, _menu(path) + "/print", params, _where_to_queries(where, query), proplist)


@mcp.tool()
def ros_add(path: str, properties: dict[str, Any], router: str | None = None) -> dict:
    """Add an item, e.g. path='/ip/address', properties={'address': '10.0.0.1/24', 'interface': 'ether2'}.
    Returns the new .id."""
    res = _run(router, _menu(path) + "/add", properties)
    return {"ok": True, "id": res[-1].get("ret") if res else None}


@mcp.tool()
def ros_set(path: str, properties: dict[str, Any], item: str | None = None, router: str | None = None) -> dict:
    """Change an item. item: '.id' like '*3', a name, or 'key=value'. Omit item for single-value menus
    such as '/system/identity', '/ip/dns', '/system/clock'."""
    menu = _menu(path)
    params = dict(properties)
    if item:
        params = {".id": _resolve_id(router, menu, item), **params}
    _run(router, menu + "/set", params)
    return {"ok": True}


@mcp.tool()
def ros_remove(path: str, item: str, router: str | None = None) -> dict:
    """Remove an item by .id, name, or 'key=value' (e.g. path='/ip/address', item='address=10.0.0.1/24')."""
    menu = _menu(path)
    iid = _resolve_id(router, menu, item)
    _run(router, menu + "/remove", {".id": iid})
    return {"ok": True, "removed": iid}


@mcp.tool()
def ros_enable(path: str, item: str, router: str | None = None) -> dict:
    """Enable an item (interface, rule, route ...)."""
    menu = _menu(path)
    _run(router, menu + "/enable", {".id": _resolve_id(router, menu, item)})
    return {"ok": True}


@mcp.tool()
def ros_disable(path: str, item: str, router: str | None = None) -> dict:
    """Disable an item (interface, rule, route ...)."""
    menu = _menu(path)
    _run(router, menu + "/disable", {".id": _resolve_id(router, menu, item)})
    return {"ok": True}


@mcp.tool()
def ros_command(command: str, params: dict[str, Any] | None = None, router: str | None = None,
                query: list[str] | None = None, proplist: list[str] | None = None) -> list[dict]:
    """Run ANY RouterOS API command: full path including the verb.
    Examples: '/system/reboot', '/tool/ping' {'address': '8.8.8.8', 'count': 3},
    '/ip/firewall/filter/move' {'numbers': '*5', 'destination': '*2'}, '/system/package/update/check-for-updates',
    '/interface/monitor-traffic' {'interface': 'ether1', 'once': ''}, '/tool/traceroute' {'address':'1.1.1.1','count':1}.
    Long-running commands need 'count' / 'once' / 'duration' so they end.
    """
    return _run(router, _menu(command), params, query, proplist)


@mcp.tool()
def ros_cli(command: str, router: str | None = None, timeout: float = 120) -> str:
    """Run plain terminal commands over SSH and return the text output, exactly as typed in the WinBox
    'New Terminal'. Multiple commands can be separated by ';' or newlines. E.g. '/ip route print detail',
    '/export hide-sensitive', ':put [/system resource get cpu-load]'."""
    return _ssh_exec(router, command, timeout)


@mcp.tool()
def ros_export(router: str | None = None, path: str = "", terse: bool = True, show_sensitive: bool = False) -> str:
    """Export the configuration as a .rsc script (whole config, or one menu like '/ip firewall')."""
    p = path.strip().replace("/", " ").strip()
    cmd = (f"/{p} " if p else "/") + "export" + (" terse" if terse else "") + (" show-sensitive" if show_sensitive else "")
    return _ssh_exec(router, cmd)


@mcp.tool()
def system_overview(router: str | None = None) -> dict:
    """Identity, RouterOS version, board, CPU/RAM/uptime, interfaces, IP addresses and routes summary."""
    out: dict[str, Any] = {}
    out["identity"] = (_run(router, "/system/identity/print") or [{}])[0].get("name")
    out["resource"] = (_run(router, "/system/resource/print") or [{}])[0]
    try:
        out["routerboard"] = (_run(router, "/system/routerboard/print") or [{}])[0]
    except RuntimeError:
        pass
    out["interfaces"] = _run(router, "/interface/print",
                             proplist=["name", "type", "running", "disabled", "mac-address", "comment"])
    out["addresses"] = _run(router, "/ip/address/print", proplist=["address", "interface", "disabled", "dynamic"])
    out["routes"] = _run(router, "/ip/route/print",
                         proplist=["dst-address", "gateway", "distance", "active", "dynamic"])
    return out


@mcp.tool()
def ros_ping(address: str, router: str | None = None, count: int = 4, src_address: str | None = None,
             interface: str | None = None, routing_table: str | None = None) -> list[dict]:
    """Ping from the router."""
    params = {"address": address, "count": count, "src-address": src_address, "interface": interface,
              "routing-table": routing_table}
    return _run(router, "/ping", params)


@mcp.tool()
def ros_backup(name: str = "mcp-backup", router: str | None = None, password: str | None = None) -> dict:
    """Create a binary backup (/system backup save) on the router; download it with ros_file_download."""
    _run(router, "/system/backup/save", {"name": name, "password": password} if password else
         {"name": name, "dont-encrypt": "yes"})
    return {"ok": True, "file": name + ".backup"}


@mcp.tool()
def ros_file_download(remote_name: str, local_path: str, router: str | None = None) -> str:
    """Download a file from the router (SFTP), e.g. a .backup or .rsc."""
    cli = _ssh(router)
    try:
        sftp = cli.open_sftp()
        sftp.get(remote_name, local_path)
    finally:
        cli.close()
    return f"saved {remote_name} -> {local_path}"


@mcp.tool()
def ros_file_upload(local_path: str, remote_name: str | None = None, router: str | None = None) -> str:
    """Upload a file to the router (SFTP), e.g. an .npk package, a .rsc to /import, or a certificate."""
    cli = _ssh(router)
    try:
        sftp = cli.open_sftp()
        sftp.put(local_path, remote_name or os.path.basename(local_path))
    finally:
        cli.close()
    return f"uploaded {local_path}"


def main() -> None:
    _load_config()
    mcp.run()


if __name__ == "__main__":
    main()
