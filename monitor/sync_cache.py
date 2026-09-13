from __future__ import annotations

import threading
import time
from typing import Any

from monitor import store
from monitor.config import Topology, load_topology
from monitor.status import enrich, sync_progress_pair

_lock = threading.Lock()
_cached: list[dict[str, Any]] | None = None
_cached_at: float = 0.0
_cached_key: str = ""

DEFAULT_TTL_SECONDS = 45
DEFAULT_TABLE_LIMIT = 500


def invalidate() -> None:
    global _cached, _cached_at, _cached_key
    with _lock:
        _cached = None
        _cached_at = 0.0
        _cached_key = ""


def _trim_tables(sync: dict[str, Any], table_limit: int) -> dict[str, Any]:
    if table_limit <= 0:
        return sync
    tables = sync.get("tables") or []
    total = len(tables)
    if total <= table_limit:
        sync["tables_total"] = total
        sync["tables_truncated"] = 0
        return sync
    out = dict(sync)
    out["tables"] = tables[:table_limit]
    out["tables_total"] = total
    out["tables_truncated"] = total - table_limit
    return out


def sync_clones(
    topology: Topology | None = None,
    *,
    ttl: float = DEFAULT_TTL_SECONDS,
    table_limit: int = DEFAULT_TABLE_LIMIT,
    force: bool = False,
) -> list[dict[str, Any]]:
    global _cached, _cached_at, _cached_key
    topology = topology or load_topology()
    key = f"{table_limit}"
    now = time.monotonic()
    with _lock:
        if (
            not force
            and _cached is not None
            and _cached_key == key
            and (now - _cached_at) < ttl
        ):
            return _cached

    if not topology.source:
        return []

    raw = store.latest_inventories()
    if not raw:
        raw = store.latest_snapshots()

    cloning = {job["target"] for job in store.active_jobs()}
    master_payload = enrich(
        raw.get(topology.source.name)
        or {"name": topology.source.name, "online": False, "databases": [], "tables": [], "size_bytes": 0},
        topology,
    )
    items: list[dict[str, Any]] = []
    for node in topology.clones:
        clone_payload = enrich(
            raw.get(node.name)
            or {
                "name": node.name,
                "online": False,
                "databases": [],
                "tables": [],
                "size_bytes": 0,
                "role": "clone",
            },
            topology,
            cloning=node.name in cloning,
        )
        sync = _trim_tables(sync_progress_pair(master_payload, clone_payload), table_limit)
        if node.name in cloning and sync.get("size_sync_percent") is not None:
            sync["live_size_percent"] = sync["size_sync_percent"]
        items.append(
            {
                "clone": node.name,
                "host": node.host,
                "display_host": node.display_host,
                "upstream": node.replicate_from,
                "clone_status": clone_payload.get("clone_status") or "UNKNOWN",
                "sync": sync,
            }
        )

    with _lock:
        _cached = items
        _cached_at = now
        _cached_key = key
    return items
