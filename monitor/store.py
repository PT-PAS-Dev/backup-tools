from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from monitor.config import sqlite_path

JAKARTA = ZoneInfo("Asia/Jakarta")
_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    server TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_server_id ON snapshots(server, id);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    server TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_lookup ON metrics(metric, server, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    type TEXT NOT NULL,
    server TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    cooldown_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts(type, server, fingerprint, ts);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    server TEXT,
    result TEXT,
    message TEXT,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS clone_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT,
    current_database TEXT,
    copied_bytes INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    message TEXT,
    log TEXT
);

CREATE TABLE IF NOT EXISTS inventories (
    server TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(JAKARTA).isoformat(timespec="seconds")


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or sqlite_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init() -> None:
    global _initialized
    with _lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def _conn() -> sqlite3.Connection:
    if not _initialized:
        init()
    return _connect()


def save_snapshot(server: str, payload: dict[str, Any], ts: str | None = None) -> None:
    compact = dict(payload)
    compact.pop("tables", None)
    blob = json.dumps(compact, default=str)
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO snapshots (ts, server, payload) VALUES (?, ?, ?)",
                (ts or now_iso(), server, blob),
            )
            conn.commit()
        finally:
            conn.close()


def save_inventory(server: str, payload: dict[str, Any], ts: str | None = None) -> None:
    blob = json.dumps(payload, default=str)
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO inventories (server, ts, payload) VALUES (?, ?, ?)
                ON CONFLICT(server) DO UPDATE SET ts = excluded.ts, payload = excluded.payload
                """,
                (server, ts or now_iso(), blob),
            )
            conn.commit()
        finally:
            conn.close()


def latest_inventories() -> dict[str, dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute("SELECT server, ts, payload FROM inventories").fetchall()
        finally:
            conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        payload["_ts"] = row["ts"]
        result[row["server"]] = payload
    return result


def save_metrics(server: str, metrics: dict[str, float | None], ts: str | None = None, extra: str = "") -> None:
    stamp = ts or now_iso()
    rows = [(stamp, server, key, value, extra) for key, value in metrics.items()]
    with _lock:
        conn = _conn()
        try:
            conn.executemany(
                "INSERT INTO metrics (ts, server, metric, value, extra) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()


def latest_snapshots() -> dict[str, dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                """
                SELECT s.server, s.ts, s.payload
                FROM snapshots s
                INNER JOIN (
                    SELECT server, MAX(id) AS max_id FROM snapshots GROUP BY server
                ) latest ON latest.max_id = s.id
                """
            ).fetchall()
        finally:
            conn.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        payload["_ts"] = row["ts"]
        result[row["server"]] = payload
    return result


def snapshot_history(server: str, limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT ts, payload FROM snapshots WHERE server = ? ORDER BY id DESC LIMIT ?",
                (server, limit),
            ).fetchall()
        finally:
            conn.close()
    items = []
    for row in rows:
        payload = json.loads(row["payload"])
        payload["_ts"] = row["ts"]
        items.append(payload)
    return items


def metrics_series(metric: str, since: str, servers: list[str] | None = None) -> dict[str, list[list[Any]]]:
    sql = "SELECT ts, server, value FROM metrics WHERE metric = ? AND ts >= ? ORDER BY ts"
    params: list[Any] = [metric, since]
    if servers:
        placeholders = ",".join("?" for _ in servers)
        sql = f"SELECT ts, server, value FROM metrics WHERE metric = ? AND ts >= ? AND server IN ({placeholders}) ORDER BY ts"
        params.extend(servers)
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    series: dict[str, list[list[Any]]] = {}
    for row in rows:
        series.setdefault(row["server"], []).append([row["ts"], row["value"]])
    return series


def metric_value_at(server: str, metric: str, around: str) -> float | None:
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                """
                SELECT value FROM metrics
                WHERE server = ? AND metric = ? AND ts <= ?
                ORDER BY ts DESC LIMIT 1
                """,
                (server, metric, around),
            ).fetchone()
        finally:
            conn.close()
    if row is None or row["value"] is None:
        return None
    return float(row["value"])


def insert_alert(
    alert_type: str,
    server: str,
    fingerprint: str,
    severity: str,
    message: str,
    cooldown_until: str,
) -> int:
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO alerts (ts, type, server, fingerprint, severity, message, cooldown_until)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), alert_type, server, fingerprint, severity, message, cooldown_until),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def latest_alert(alert_type: str, server: str, fingerprint: str) -> dict[str, Any] | None:
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(
                """
                SELECT ts, cooldown_until, message, severity
                FROM alerts
                WHERE type = ? AND server = ? AND fingerprint = ?
                ORDER BY id DESC LIMIT 1
                """,
                (alert_type, server, fingerprint),
            ).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def list_alerts(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                """
                SELECT id, ts, type, server, fingerprint, severity, message
                FROM alerts ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(row) for row in rows]


def insert_audit(username: str, action: str, server: str, result: str, message: str, ip: str) -> None:
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                """
                INSERT INTO audit (ts, username, action, server, result, message, ip)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), username, action, server, result, message, ip),
            )
            conn.commit()
        finally:
            conn.close()


def create_job(source: str, target: str, total_bytes: int, message: str) -> int:
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO clone_jobs (ts, source, target, status, phase, copied_bytes, total_bytes, message, log)
                VALUES (?, ?, ?, 'RUNNING', 'PRECHECK', 0, ?, ?, '')
                """,
                (now_iso(), source, target, total_bytes, message),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [job_id]
    with _lock:
        conn = _conn()
        try:
            conn.execute(f"UPDATE clone_jobs SET {assignments} WHERE id = ?", values)
            conn.commit()
        finally:
            conn.close()


def append_job_log(job_id: int, line: str) -> None:
    stamp = datetime.now(JAKARTA).strftime("%H:%M:%S")
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT log FROM clone_jobs WHERE id = ?", (job_id,)).fetchone()
            previous = row["log"] if row and row["log"] else ""
            log = (previous + f"[{stamp}] {line}\n")[-20000:]
            conn.execute("UPDATE clone_jobs SET log = ? WHERE id = ?", (log, job_id))
            conn.commit()
        finally:
            conn.close()


def get_job(job_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM clone_jobs WHERE id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def latest_job(target: str | None = None) -> dict[str, Any] | None:
    sql = "SELECT * FROM clone_jobs ORDER BY id DESC LIMIT 1"
    params: tuple[Any, ...] = ()
    if target:
        sql = "SELECT * FROM clone_jobs WHERE target = ? ORDER BY id DESC LIMIT 1"
        params = (target,)
    with _lock:
        conn = _conn()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def active_jobs() -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT * FROM clone_jobs WHERE status = 'RUNNING' ORDER BY id DESC"
            ).fetchall()
        finally:
            conn.close()
    return [dict(row) for row in rows]


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT * FROM clone_jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(row) for row in rows]


def prune(days: int = 8) -> None:
    cutoff = (datetime.now(JAKARTA) - timedelta(days=days)).isoformat(timespec="seconds")
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
