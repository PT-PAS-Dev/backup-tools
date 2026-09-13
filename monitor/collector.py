from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from monitor import alerts, store
from monitor.config import Node, Topology, load_topology
from monitor.mariadb import collect as collect_mariadb
from monitor.ssh import collect_os
from monitor.status import enrich
from monitor import sync_cache

log = logging.getLogger("clone-monitor")
_poll_count = 0
_poll_gate = threading.Lock()
_poll_running = False

# Full table inventory is expensive on .94 — run less often.
TABLE_INVENTORY_EVERY = 12
OS_METRICS_EVERY = 4


def _should_include_tables(node: Node, poll_n: int, cloning_targets: set[str]) -> bool:
    if poll_n == 1:
        return True
    if node.name in cloning_targets:
        return poll_n % max(6, TABLE_INVENTORY_EVERY // 2) == 0
    return poll_n % TABLE_INVENTORY_EVERY == 0


def _collect_node(
    node: Node,
    *,
    include_tables: bool,
    include_os: bool,
) -> tuple[str, dict[str, Any]]:
    payload = collect_mariadb(node, include_tables=include_tables)
    if include_os:
        payload["os"] = collect_os(node)
    else:
        payload["os"] = payload.get("os") or {"available": False, "reason": "skipped this poll"}
    return node.name, payload


def poll_once(topology: Topology | None = None) -> dict[str, dict[str, Any]]:
    global _poll_count, _poll_running
    if not _poll_gate.acquire(blocking=False):
        log.warning("poll skipped — previous cycle still running")
        latest = store.latest_snapshots()
        return latest if latest else {}

    _poll_running = True
    try:
        topology = topology or load_topology()
        snapshots: dict[str, dict[str, Any]] = {}
        cloning_targets = {job["target"] for job in store.active_jobs()}
        _poll_count += 1
        poll_n = _poll_count
        include_os = poll_n == 1 or poll_n % OS_METRICS_EVERY == 0
        inventory_updated = False

        nodes = topology.nodes()
        futures = {}
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(nodes)))) as pool:
            for node in nodes:
                inc_tables = _should_include_tables(node, poll_n, cloning_targets)
                futures[
                    pool.submit(
                        _collect_node,
                        node,
                        include_tables=inc_tables,
                        include_os=include_os,
                    )
                ] = (node, inc_tables)

            for future in as_completed(futures):
                node, inc_tables = futures[future]
                try:
                    name, payload = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.exception("collect failed for %s", node.name)
                    payload = {
                        "name": node.name,
                        "host": node.host,
                        "online": False,
                        "error": str(exc),
                        "databases": [],
                        "tables": [],
                        "size_bytes": 0,
                    }
                    name = node.name
                    inc_tables = False

                if inc_tables:
                    store.save_inventory(name, payload)
                    inventory_updated = True
                store.save_snapshot(name, payload)

                metrics: dict[str, float | None] = {
                    "online": 1.0 if payload.get("online") else 0.0,
                    "size_bytes": float(payload.get("size_bytes") or 0),
                    "lag_seconds": None,
                    "cpu_percent": None,
                    "ram_percent": None,
                    "disk_percent": None,
                    "mariadb_uptime": float(payload["mariadb_uptime_seconds"])
                    if payload.get("mariadb_uptime_seconds") is not None
                    else None,
                }
                slave = payload.get("slave")
                if isinstance(slave, dict) and slave.get("seconds_behind") is not None:
                    try:
                        metrics["lag_seconds"] = float(slave["seconds_behind"])
                    except (TypeError, ValueError):
                        metrics["lag_seconds"] = None
                os_metrics = payload.get("os") or {}
                if os_metrics.get("available"):
                    metrics["cpu_percent"] = os_metrics.get("cpu_percent")
                    metrics["ram_percent"] = os_metrics.get("ram_percent")
                    metrics["disk_percent"] = os_metrics.get("disk_percent")
                master = payload.get("master") or {}
                gtid = payload.get("gtid") or {}
                extra_parts = [
                    f"binlog={master.get('File') or ''}:{master.get('Position') or ''}",
                    f"gtid={gtid.get('current_pos') or gtid.get('slave_pos') or ''}",
                    f"repl={_repl_flag(slave)}",
                ]
                extra = " | ".join(extra_parts)
                store.save_metrics(name, metrics, extra=extra)
                snapshots[name] = enrich(payload, topology, cloning=name in cloning_targets)

        if inventory_updated:
            sync_cache.invalidate()

        try:
            alerts.evaluate(topology, snapshots)
        except Exception:
            log.exception("alert evaluation failed")
        try:
            store.prune(8)
        except Exception:
            log.exception("metric prune failed")
        return snapshots
    finally:
        _poll_running = False
        _poll_gate.release()


def _repl_flag(slave: Any) -> str:
    if not isinstance(slave, dict) or slave.get("error"):
        return "none"
    return f"{slave.get('io_running')}/{slave.get('sql_running')}"


def compact_node(payload: dict[str, Any]) -> dict[str, Any]:
    slim = dict(payload)
    slim.pop("tables", None)
    return slim


def cached_overview(topology: Topology | None = None) -> dict[str, Any]:
    topology = topology or load_topology()
    raw = store.latest_snapshots()
    cloning_targets = {job["target"] for job in store.active_jobs()}
    nodes = []
    for node in topology.nodes():
        payload = raw.get(node.name) or {
            "name": node.name,
            "host": node.host,
            "port": node.port,
            "role": node.role,
            "os_type": node.os,
            "online": False,
            "error": "No sample yet",
            "databases": [],
            "tables": [],
            "size_bytes": 0,
            "os": {"available": False, "reason": "No sample yet"},
            "mysql_user": node.user or "—",
            "mysql_port": node.port,
        }
        payload.setdefault("name", node.name)
        payload.setdefault("host", node.host)
        payload.setdefault("role", node.role)
        payload["display_host"] = node.display_host
        payload["title"] = node.title
        payload["replicate_from"] = node.replicate_from
        payload["mysql_user"] = node.user or "—"
        payload["mysql_port"] = node.port
        enriched = enrich(payload, topology, cloning=node.name in cloning_targets)
        enriched["sampled_at"] = payload.get("_ts") or ""
        nodes.append(compact_node(enriched))
    return {
        "missing": topology.missing,
        "nodes": nodes,
        "source": next((item for item in nodes if item.get("role") == "source"), None),
        "clones": [item for item in nodes if item.get("role") == "clone"],
        "jobs": store.active_jobs(),
        "alerts": store.list_alerts(limit=8),
    }
