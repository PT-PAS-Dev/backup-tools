from __future__ import annotations

from typing import Any

from monitor.config import MonitorThresholds, Topology


def format_size(size_bytes: int | float | None) -> str:
    if size_bytes is None:
        return "N/A"
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def clone_status(payload: dict[str, Any], thresholds: MonitorThresholds, cloning: bool = False) -> str:
    if cloning:
        return "CLONING"
    if not payload.get("online"):
        return "OFFLINE"
    if payload.get("role") == "source":
        return "SOURCE"
    slave = payload.get("slave")
    if isinstance(slave, dict) and slave.get("error"):
        return "UNKNOWN"
    if not slave:
        return "UNKNOWN"
    io_running = str(slave.get("io_running") or "")
    sql_running = str(slave.get("sql_running") or "")
    last_error = str(slave.get("last_error") or slave.get("last_io_error") or slave.get("last_sql_error") or "")
    if last_error and (io_running != "Yes" or sql_running != "Yes"):
        return "ERROR"
    if io_running != "Yes" or sql_running != "Yes":
        if io_running in {"Connecting", "Preparing"} or sql_running in {"Connecting", "Preparing"}:
            return "SYNCING"
        return "ERROR"
    lag = slave.get("seconds_behind")
    if lag is None:
        return "SYNCING"
    try:
        lag_i = int(lag)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if lag_i > thresholds.lag_warning_seconds:
        return "LAGGING"
    return "SYNCHRONIZED"


def health_score(payload: dict[str, Any], thresholds: MonitorThresholds, cloning: bool = False) -> dict[str, str]:
    status = clone_status(payload, thresholds, cloning=cloning)
    if payload.get("role") == "source":
        if not payload.get("online"):
            return {"label": "OFFLINE", "tone": "crit", "status": status}
        if payload.get("read_only"):
            return {"label": "READ ONLY", "tone": "warn", "status": status}
        return {"label": "HEALTHY", "tone": "ok", "status": status}
    mapping = {
        "CLONING": ("CLONING", "info"),
        "SYNCHRONIZED": ("HEALTHY", "ok"),
        "SYNCING": ("SYNCING", "info"),
        "LAGGING": ("LAGGING", "warn"),
        "ERROR": ("REPLICATION ERROR", "crit"),
        "OFFLINE": ("OFFLINE", "crit"),
        "UNKNOWN": ("UNKNOWN", "muted"),
        "SOURCE": ("HEALTHY", "ok"),
    }
    label, tone = mapping.get(status, ("UNKNOWN", "muted"))
    os_metrics = payload.get("os") or {}
    if os_metrics.get("available"):
        disk = os_metrics.get("disk_percent")
        if disk is not None and disk >= (100 - thresholds.disk_low_percent) and tone == "ok":
            return {"label": "DISK LOW", "tone": "warn", "status": status}
    return {"label": label, "tone": tone, "status": status}


def replication_health(slave: dict[str, Any] | None, thresholds: MonitorThresholds) -> dict[str, Any]:
    if not slave:
        return {"label": "NOT CONFIGURED", "tone": "muted", "lag_text": "n/a"}
    if slave.get("error"):
        return {"label": "UNKNOWN", "tone": "muted", "lag_text": "n/a"}
    io_running = slave.get("io_running")
    sql_running = slave.get("sql_running")
    lag = slave.get("seconds_behind")
    if io_running != "Yes" or sql_running != "Yes":
        return {"label": "CRITICAL", "tone": "crit", "lag_text": "Replication stopped"}
    if lag is None:
        return {"label": "SYNCING", "tone": "info", "lag_text": "unknown"}
    lag_i = int(lag)
    if lag_i > thresholds.lag_warning_seconds:
        return {"label": "WARNING", "tone": "warn", "lag_text": f"{lag_i} sec"}
    return {"label": "HEALTHY", "tone": "ok", "lag_text": f"{lag_i} sec"}


def binlog_retention_risk(source: dict[str, Any], replica: dict[str, Any], ratio: float) -> dict[str, Any] | None:
    if not source.get("online") or not replica.get("online"):
        return None
    slave = replica.get("slave") or {}
    lag = slave.get("seconds_behind")
    expire = source.get("variables", {}).get("expire_logs_days") or ""
    try:
        expire_days = float(expire)
    except (TypeError, ValueError):
        expire_days = None
    if expire_days is None or expire_days <= 0 or lag is None:
        return None
    lag_days = float(lag) / 86400.0
    risk = "LOW"
    if lag_days >= expire_days * 0.8:
        risk = "HIGH"
    elif lag_days >= expire_days * ratio:
        risk = "MEDIUM"
    else:
        return None
    return {
        "source": source.get("name"),
        "replica": replica.get("name"),
        "retention_days": expire_days,
        "lag_days": round(lag_days, 2),
        "risk": risk,
    }


def sizes_match(a: int, b: int, tolerance: float = 0.02, floor: int = 16 * 1024 * 1024) -> bool:
    delta = abs(a - b)
    if delta <= floor:
        return True
    baseline = max(a, b, 1)
    return (delta / baseline) <= tolerance


def verify_pair(source: dict[str, Any], clone: dict[str, Any]) -> dict[str, Any]:
    src_dbs = {item["name"]: item for item in source.get("databases") or []}
    dst_dbs = {item["name"]: item for item in clone.get("databases") or []}
    src_tables = {(t["schema"], t["name"]): t for t in source.get("tables") or []}
    dst_tables = {(t["schema"], t["name"]): t for t in clone.get("tables") or []}

    missing_dbs = sorted(set(src_dbs) - set(dst_dbs))
    extra_dbs = sorted(set(dst_dbs) - set(src_dbs))
    missing_tables = sorted(f"{s}.{n}" for s, n in set(src_tables) - set(dst_tables))
    extra_tables = sorted(f"{s}.{n}" for s, n in set(dst_tables) - set(src_tables))

    engine_diffs = []
    for key, src_table in src_tables.items():
        dst_table = dst_tables.get(key)
        if not dst_table:
            continue
        if (src_table.get("engine") or "") != (dst_table.get("engine") or ""):
            engine_diffs.append(
                {
                    "table": f"{key[0]}.{key[1]}",
                    "source": src_table.get("engine") or "",
                    "clone": dst_table.get("engine") or "",
                }
            )

    size_ok = sizes_match(int(source.get("size_bytes") or 0), int(clone.get("size_bytes") or 0))
    db_ok = not missing_dbs and not extra_dbs
    table_ok = not missing_tables and not extra_tables
    engine_ok = not engine_diffs
    gtid_note = ""
    src_gtid = (source.get("gtid") or {}).get("binlog_pos") or (source.get("gtid") or {}).get("current_pos") or ""
    dst_gtid = (clone.get("gtid") or {}).get("slave_pos") or (clone.get("gtid") or {}).get("current_pos") or ""
    slave = clone.get("slave") or {}
    repl_ok = bool(slave) and slave.get("io_running") == "Yes" and slave.get("sql_running") == "Yes"
    if src_gtid and dst_gtid:
        gtid_note = "present on both"
    elif src_gtid or dst_gtid:
        gtid_note = "mismatch / incomplete"
    else:
        gtid_note = "not advertised"

    overall = "MATCH" if source.get("online") and clone.get("online") and db_ok and table_ok and engine_ok and size_ok else "DIFFERENCE"
    if not source.get("online") or not clone.get("online"):
        overall = "DIFFERENCE"

    return {
        "overall": overall,
        "source_online": bool(source.get("online")),
        "clone_online": bool(clone.get("online")),
        "source_size": int(source.get("size_bytes") or 0),
        "clone_size": int(clone.get("size_bytes") or 0),
        "source_db_count": len(src_dbs),
        "clone_db_count": len(dst_dbs),
        "source_table_count": len(src_tables),
        "clone_table_count": len(dst_tables),
        "missing_databases": missing_dbs,
        "extra_databases": extra_dbs,
        "missing_tables": missing_tables[:200],
        "missing_tables_more": max(0, len(missing_tables) - 200),
        "extra_tables": extra_tables[:200],
        "engine_diffs": engine_diffs[:100],
        "size_match": size_ok,
        "gtid_source": src_gtid,
        "gtid_clone": dst_gtid,
        "gtid_note": gtid_note,
        "replication_running": repl_ok,
        "replication_lag": slave.get("seconds_behind") if slave else None,
        "databases": _db_compare_rows(src_dbs, dst_dbs),
    }


def _db_compare_rows(src: dict[str, dict[str, Any]], dst: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    names = sorted(set(src) | set(dst))
    rows = []
    for name in names:
        left = src.get(name) or {}
        right = dst.get(name) or {}
        lsize = int(left.get("size_bytes") or 0)
        rsize = int(right.get("size_bytes") or 0)
        rows.append(
            {
                "name": name,
                "source_size": lsize,
                "clone_size": rsize,
                "source_tables": int(left.get("table_count") or 0),
                "clone_tables": int(right.get("table_count") or 0),
                "source_rows": int(left.get("approx_rows") or 0),
                "clone_rows": int(right.get("approx_rows") or 0),
                "match": bool(left) and bool(right) and sizes_match(lsize, rsize) and int(left.get("table_count") or 0) == int(right.get("table_count") or 0),
            }
        )
    return rows


def _table_sync_status(source_bytes: int, clone_bytes: int) -> tuple[str, int]:
    if clone_bytes <= 0:
        return "missing", 0
    if sizes_match(source_bytes, clone_bytes, tolerance=0.05, floor=64 * 1024):
        return "synced", 100
    if source_bytes <= 0:
        return "partial", 100 if clone_bytes else 0
    pct = int(min(99, clone_bytes * 100 / source_bytes))
    return "partial", pct


def rows_match(source_rows: int, clone_rows: int, tolerance: float = 0.05, floor: int = 50) -> bool:
    a = int(source_rows or 0)
    b = int(clone_rows or 0)
    if a == 0 and b == 0:
        return True
    delta = abs(a - b)
    if delta <= floor:
        return True
    baseline = max(a, b, 1)
    return (delta / baseline) <= tolerance


def row_sync_percent(source_rows: int, clone_rows: int) -> int | None:
    src = int(source_rows or 0)
    cl = int(clone_rows or 0)
    if src <= 0:
        return 100 if cl >= 0 else None
    if cl <= 0:
        return 0
    return int(min(100, round(cl * 100.0 / src)))


def table_sync_detail(source: dict[str, Any], clone: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    src_tables = {(t["schema"], t["name"]): t for t in source.get("tables") or []}
    dst_tables = {(t["schema"], t["name"]): t for t in clone.get("tables") or []}
    rows: list[dict[str, Any]] = []
    synced = partial = missing = 0
    source_bytes = 0
    matched_bytes = 0
    source_row_total = 0
    clone_row_total = 0
    table_rows_matched = 0

    for key in sorted(src_tables.keys()):
        src_t = src_tables[key]
        dst_t = dst_tables.get(key)
        sbytes = int(src_t.get("size_bytes") or 0)
        srows = int(src_t.get("approx_rows") or 0)
        source_bytes += sbytes
        source_row_total += srows
        cbytes = int(dst_t.get("size_bytes") or 0) if dst_t else 0
        crows = int(dst_t.get("approx_rows") or 0) if dst_t else 0
        clone_row_total += crows
        status, pct = _table_sync_status(sbytes, cbytes)
        rpct = row_sync_percent(srows, crows)
        rmatch = rows_match(srows, crows)
        if rmatch and dst_t:
            table_rows_matched += 1
        if status == "synced":
            synced += 1
            matched_bytes += sbytes
        elif status == "missing":
            missing += 1
        else:
            partial += 1
            matched_bytes += min(sbytes, cbytes)
        rows.append(
            {
                "database": key[0],
                "table": key[1],
                "full_name": f"{key[0]}.{key[1]}",
                "status": status,
                "source_bytes": sbytes,
                "clone_bytes": cbytes,
                "percent": pct,
                "source_rows": srows,
                "clone_rows": crows,
                "row_delta": crows - srows,
                "row_percent": rpct,
                "rows_match": rmatch and bool(dst_t),
            }
        )

    total = len(src_tables)
    summary = {
        "table_total": total,
        "table_synced": synced,
        "table_partial": partial,
        "table_missing": missing,
        "table_sync_percent": round(synced * 100.0 / total, 1) if total else None,
        "size_sync_percent": round(matched_bytes * 100.0 / source_bytes, 1) if source_bytes else None,
        "source_table_count": total,
        "clone_table_count": len(dst_tables),
        "inventory_has_tables": bool(src_tables),
        "source_row_total": source_row_total,
        "clone_row_total": clone_row_total,
        "row_sync_percent": round(clone_row_total * 100.0 / source_row_total, 1) if source_row_total else None,
        "table_row_sync_percent": round(table_rows_matched * 100.0 / total, 1) if total else None,
        "table_rows_matched": table_rows_matched,
        "rows_are_estimates": True,
    }
    return rows, summary


def sync_progress_pair(source: dict[str, Any], clone: dict[str, Any]) -> dict[str, Any]:
    verify = verify_pair(source, clone)
    tables, table_summary = table_sync_detail(source, clone)
    db_rows = []
    for db in verify.get("databases") or []:
        src_sz = int(db.get("source_size") or 0)
        cl_sz = int(db.get("clone_size") or 0)
        _, pct = _table_sync_status(src_sz, cl_sz)
        st = "synced" if db.get("match") else ("missing" if cl_sz <= 0 and src_sz > 0 else "partial")
        sr = int(db.get("source_rows") or 0)
        cr = int(db.get("clone_rows") or 0)
        db_rows.append(
            {
                **db,
                "sync_percent": pct,
                "sync_status": st,
                "row_percent": row_sync_percent(sr, cr),
                "rows_match": rows_match(sr, cr),
                "row_delta": cr - sr,
            }
        )

    return {
        "overall": verify.get("overall"),
        "source_online": verify.get("source_online"),
        "clone_online": verify.get("clone_online"),
        "replication_running": verify.get("replication_running"),
        "replication_lag": verify.get("replication_lag"),
        "source_size": verify.get("source_size"),
        "clone_size": verify.get("clone_size"),
        "databases": db_rows,
        "tables": tables,
        "tables_truncated": False,
        **table_summary,
    }


def mysql_connection_status(payload: dict[str, Any]) -> dict[str, str]:
    if payload.get("online"):
        version = str(payload.get("version") or "").strip()
        detail = version or "SELECT / SHOW OK"
        return {
            "status": "CONNECTED",
            "label": "CONNECTED",
            "tone": "ok",
            "detail": detail,
        }
    error = str(payload.get("error") or "").strip()
    if not error or error == "No sample yet":
        return {
            "status": "WAITING",
            "label": "WAITING",
            "tone": "muted",
            "detail": error or "Belum ada poll",
        }
    return {
        "status": "FAILED",
        "label": "FAILED",
        "tone": "crit",
        "detail": error[:280],
    }


def enrich(payload: dict[str, Any], topology: Topology, cloning: bool = False) -> dict[str, Any]:
    health = health_score(payload, topology.thresholds, cloning=cloning)
    slave = payload.get("slave") if isinstance(payload.get("slave"), dict) and not payload.get("slave", {}).get("error") else None
    payload = dict(payload)
    payload["mysql"] = mysql_connection_status(payload)
    payload["clone_status"] = clone_status(payload, topology.thresholds, cloning=cloning)
    payload["health"] = health
    payload["replication"] = replication_health(slave, topology.thresholds)
    payload["size_human"] = format_size(payload.get("size_bytes"))
    payload["uptime_human"] = format_duration(payload.get("mariadb_uptime_seconds"))
    vars_ = payload.get("variables") or {}
    payload["binlog_on"] = str(vars_.get("log_bin") or "").lower() in {"on", "1", "true"}
    payload["server_id"] = vars_.get("server_id") or ""
    payload["binlog_format"] = vars_.get("binlog_format") or ""
    payload["log_slave_updates"] = vars_.get("log_slave_updates") or ""
    payload["expire_logs_days"] = vars_.get("expire_logs_days") or ""
    master = payload.get("master") or {}
    payload["binlog_file"] = master.get("File") or ""
    payload["binlog_pos"] = master.get("Position") or ""
    os_metrics = payload.get("os") or {}
    payload["cpu_text"] = _metric_text(os_metrics, "cpu_percent", "%")
    payload["ram_text"] = _metric_text(os_metrics, "ram_percent", "%")
    payload["disk_text"] = _disk_text(os_metrics)
    payload["load_text"] = _load_text(os_metrics)
    payload["last_error"] = ""
    if slave:
        payload["last_error"] = slave.get("last_error") or slave.get("last_sql_error") or slave.get("last_io_error") or ""
        payload["last_sync"] = slave.get("last_error_time") or ""
        payload["lag_seconds"] = slave.get("seconds_behind")
    elif payload.get("error"):
        payload["last_error"] = payload.get("error") or ""
    return payload


def _metric_text(os_metrics: dict[str, Any], key: str, suffix: str) -> str:
    if not os_metrics.get("available"):
        return os_metrics.get("reason") or "N/A"
    value = os_metrics.get(key)
    if value is None:
        return "N/A"
    return f"{value}{suffix}"


def _disk_text(os_metrics: dict[str, Any]) -> str:
    if not os_metrics.get("available"):
        return os_metrics.get("reason") or "N/A"
    free = os_metrics.get("disk_free_bytes")
    percent = os_metrics.get("disk_percent")
    if free is None:
        return "N/A"
    used = f"{percent}% used" if percent is not None else ""
    return f"{format_size(free)} free · {used}".strip(" ·")


def _load_text(os_metrics: dict[str, Any]) -> str:
    if not os_metrics.get("available"):
        return os_metrics.get("reason") or "N/A"
    if os_metrics.get("load_1") is None:
        return "N/A"
    return f"{os_metrics['load_1']} / {os_metrics.get('load_5')} / {os_metrics.get('load_15')}"
