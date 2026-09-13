from __future__ import annotations

import re
from typing import Any

from monitor.config import Node

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None


def ssh_configured(node: Node) -> bool:
    return bool(node.ssh_user and (node.ssh_password or node.ssh_key))


def run(node: Node, command: str, timeout: int = 20) -> tuple[int, str, str]:
    if paramiko is None:
        raise RuntimeError("paramiko is not installed")
    if not ssh_configured(node):
        raise RuntimeError(f"{node.name}: SSH is not configured")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": node.host,
        "port": node.ssh_port,
        "username": node.ssh_user,
        "timeout": timeout,
        "auth_timeout": timeout,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if node.ssh_key:
        connect_kwargs["key_filename"] = node.ssh_key
    if node.ssh_password:
        connect_kwargs["password"] = node.ssh_password
    try:
        client.connect(**connect_kwargs)
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return code, out, err
    finally:
        client.close()


def collect_os(node: Node) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": False,
        "reason": "",
        "cpu_percent": None,
        "ram_percent": None,
        "ram_total_bytes": None,
        "ram_used_bytes": None,
        "disk_percent": None,
        "disk_total_bytes": None,
        "disk_used_bytes": None,
        "disk_free_bytes": None,
        "load_1": None,
        "load_5": None,
        "load_15": None,
        "uptime_seconds": None,
    }
    if node.os.lower().startswith("win"):
        payload["reason"] = "UNAVAILABLE — Windows host has no SSH OS collector"
        return payload
    if paramiko is None:
        payload["reason"] = "UNAVAILABLE — paramiko not installed"
        return payload
    if not ssh_configured(node):
        payload["reason"] = "UNAVAILABLE — SSH not configured"
        return payload
    try:
        code, out, err = run(
            node,
            "cat /proc/loadavg; echo '---'; cat /proc/meminfo; echo '---'; df -PB1 / | tail -n 1; echo '---'; "
            "cat /proc/uptime; echo '---'; nproc; echo '---'; "
            "grep -E 'cpu ' /proc/stat",
            timeout=15,
        )
        if code != 0:
            payload["reason"] = (err or out or f"ssh exit {code}").strip()[:300]
            return payload
        payload.update(_parse_linux_metrics(out))
        payload["available"] = True
        payload["reason"] = ""
        return payload
    except Exception as exc:  # noqa: BLE001 — collector must never crash the poller
        payload["reason"] = f"UNAVAILABLE — {exc}"
        return payload


def _parse_linux_metrics(raw: str) -> dict[str, Any]:
    parts = raw.split("---")
    result: dict[str, Any] = {}
    if parts:
        load = parts[0].split()
        if len(load) >= 3:
            result["load_1"] = float(load[0])
            result["load_5"] = float(load[1])
            result["load_15"] = float(load[2])
    meminfo = parts[1] if len(parts) > 1 else ""
    mem = _parse_meminfo(meminfo)
    result.update(mem)
    if len(parts) > 2:
        df = parts[2].split()
        if len(df) >= 4:
            total = int(df[1])
            used = int(df[2])
            free = int(df[3])
            result["disk_total_bytes"] = total
            result["disk_used_bytes"] = used
            result["disk_free_bytes"] = free
            result["disk_percent"] = round(used * 100.0 / total, 1) if total else None
    if len(parts) > 3:
        up = parts[3].split()
        if up:
            result["uptime_seconds"] = int(float(up[0]))
    nproc = 1
    if len(parts) > 4:
        try:
            nproc = max(1, int(parts[4].strip().splitlines()[0]))
        except (TypeError, ValueError):
            nproc = 1
    if result.get("load_1") is not None:
        result["cpu_percent"] = round(min(100.0, float(result["load_1"]) * 100.0 / nproc), 1)
    return result


def _parse_meminfo(text: str) -> dict[str, Any]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if not match:
            continue
        values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None:
        return {}
    used = total - available if available is not None else total - values.get("MemFree", 0)
    return {
        "ram_total_bytes": total,
        "ram_used_bytes": used,
        "ram_percent": round(used * 100.0 / total, 1) if total else None,
    }
