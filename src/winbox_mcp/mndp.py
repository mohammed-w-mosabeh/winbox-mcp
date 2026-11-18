"""MikroTik Neighbor Discovery Protocol (what WinBox's "Neighbors" tab shows). UDP 5678."""
from __future__ import annotations

import socket
import time

_TLV = {1: "mac", 5: "identity", 7: "version", 8: "platform", 10: "uptime", 11: "software_id",
        12: "board", 14: "unpack", 15: "ipv6", 16: "interface", 17: "ipv4"}


def _parse(data: bytes, src: str) -> dict:
    out: dict = {"source": src}
    i = 4  # header: 2 bytes type/ttl + 2 bytes seq
    while i + 4 <= len(data):
        t = int.from_bytes(data[i:i + 2], "big")
        n = int.from_bytes(data[i + 2:i + 4], "big")
        v = data[i + 4:i + 4 + n]
        i += 4 + n
        key = _TLV.get(t)
        if not key:
            continue
        if key == "mac":
            out[key] = ":".join(f"{b:02X}" for b in v)
        elif key == "uptime" and n == 4:
            secs = int.from_bytes(v, "little")
            out[key] = f"{secs // 86400}d{secs % 86400 // 3600:02}:{secs % 3600 // 60:02}:{secs % 60:02}"
        elif key == "ipv4" and n == 4:
            out[key] = socket.inet_ntoa(v)
        elif key == "ipv6" and n == 16:
            out[key] = socket.inet_ntop(socket.AF_INET6, v)
        elif key == "unpack":
            out[key] = v[0] if v else None
        else:
            out[key] = v.decode("utf-8", "replace")
    return out


def discover(seconds: float = 5.0) -> list[dict]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind(("", 5678))
    except OSError:
        s.bind(("", 0))  # someone (e.g. WinBox) holds 5678: we still get unicast answers to our probe
    s.settimeout(0.5)
    found: dict[str, dict] = {}
    end = time.time() + seconds
    next_probe = 0.0
    try:
        while time.time() < end:
            if time.time() >= next_probe:
                try:
                    s.sendto(b"\x00\x00\x00\x00", ("255.255.255.255", 5678))
                except OSError:
                    pass
                next_probe = time.time() + 2
            try:
                data, (ip, _) = s.recvfrom(2048)
            except socket.timeout:
                continue
            if len(data) <= 4:
                continue  # a probe, not an announcement
            d = _parse(data, ip)
            found[d.get("mac", ip) + d.get("interface", "")] = d
    finally:
        s.close()
    return sorted(found.values(), key=lambda d: (d.get("ipv4") or d["source"]))
