from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from monitor.config import Topology
from monitor import store
from monitor.status import binlog_retention_risk

JAKARTA = ZoneInfo("Asia/Jakarta")


def evaluate(topology: Topology, snapshots: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    source_name = topology.source.name if topology.source else ""
    source = snapshots.get(source_name) or {}
    thresh = topology.thresholds
    cooldown = timedelta(minutes=thresh.alert_cooldown_minutes)

    if topology.source:
        if not source.get("online"):
            created.extend(_maybe_emit("SOURCE_DOWN", source_name, "down", "crit", f"Source {source_name} is unreachable", cooldown))
        else:
            created.extend(_maybe_emit("SOURCE_DOWN", source_name, "down", "crit", "", cooldown, recovered=True))

    for clone in topology.clones:
        snap = snapshots.get(clone.name) or {}
        if not snap.get("online"):
            created.extend(_maybe_emit("CLONE_DOWN", clone.name, "down", "crit", f"Clone {clone.name} is unreachable", cooldown))
        else:
            created.extend(_maybe_emit("CLONE_DOWN", clone.name, "down", "crit", "", cooldown, recovered=True))

        slave = snap.get("slave") if isinstance(snap.get("slave"), dict) else None
        if snap.get("online") and slave and not slave.get("error"):
            io_running = slave.get("io_running")
            sql_running = slave.get("sql_running")
            if io_running != "Yes" or sql_running != "Yes":
                created.extend(
                    _maybe_emit(
                        "REPLICATION_STOPPED",
                        clone.name,
                        f"{io_running}/{sql_running}",
                        "crit",
                        f"Replication stopped on {clone.name} (IO={io_running} SQL={sql_running})",
                        cooldown,
                    )
                )
            else:
                created.extend(_maybe_emit("REPLICATION_STOPPED", clone.name, f"{io_running}/{sql_running}", "crit", "", cooldown, recovered=True))

            last_error = slave.get("last_error") or slave.get("last_sql_error") or slave.get("last_io_error") or ""
            if last_error:
                created.extend(
                    _maybe_emit(
                        "REPLICATION_ERROR",
                        clone.name,
                        last_error[:180],
                        "crit",
                        f"{clone.name}: {last_error[:300]}",
                        cooldown,
                    )
                )

            lag = slave.get("seconds_behind")
            if isinstance(lag, int) and lag > thresh.lag_warning_seconds:
                created.extend(
                    _maybe_emit(
                        "REPLICATION_LAG",
                        clone.name,
                        str(lag // max(thresh.lag_warning_seconds, 1)),
                        "warn",
                        f"{clone.name} lag {lag} sec",
                        cooldown,
                    )
                )

            risk = binlog_retention_risk(source, snap, thresh.binlog_risk_ratio) if source else None
            if risk:
                created.extend(
                    _maybe_emit(
                        "BINLOG_RETENTION_RISK",
                        clone.name,
                        risk["risk"],
                        "crit" if risk["risk"] == "HIGH" else "warn",
                        (
                            f"Source {risk['source']} retention {risk['retention_days']} days; "
                            f"{clone.name} lag {risk['lag_days']} days; risk {risk['risk']}"
                        ),
                        cooldown,
                    )
                )

        os_metrics = snap.get("os") or {}
        if os_metrics.get("available"):
            disk_free_pct = None
            if os_metrics.get("disk_percent") is not None:
                disk_free_pct = 100.0 - float(os_metrics["disk_percent"])
            if disk_free_pct is not None and disk_free_pct < thresh.disk_low_percent:
                created.extend(
                    _maybe_emit(
                        "DISK_LOW",
                        clone.name,
                        "disk",
                        "warn",
                        f"{clone.name} disk free {disk_free_pct:.1f}%",
                        cooldown,
                    )
                )
            cpu = os_metrics.get("cpu_percent")
            if cpu is not None and cpu >= thresh.cpu_high_percent:
                created.extend(
                    _maybe_emit(
                        "CPU_HIGH",
                        clone.name,
                        "cpu",
                        "warn",
                        f"{clone.name} CPU {cpu}%",
                        cooldown,
                    )
                )
            ram = os_metrics.get("ram_percent")
            if ram is not None and ram >= thresh.ram_high_percent:
                created.extend(
                    _maybe_emit(
                        "RAM_HIGH",
                        clone.name,
                        "ram",
                        "warn",
                        f"{clone.name} RAM {ram}%",
                        cooldown,
                    )
                )

        if snap.get("online") and source.get("online"):
            current = float(snap.get("size_bytes") or 0)
            past_ts = (datetime.now(JAKARTA) - timedelta(hours=24)).isoformat(timespec="seconds")
            previous = store.metric_value_at(clone.name, "size_bytes", past_ts)
            if previous and previous > 0:
                growth = (current - previous) * 100.0 / previous
                if growth >= thresh.growth_percent_24h:
                    created.extend(
                        _maybe_emit(
                            "DATABASE_GROWTH",
                            clone.name,
                            str(int(growth)),
                            "info",
                            f"{clone.name} database size grew {growth:.1f}% in 24h",
                            cooldown,
                        )
                    )
    return created


def _maybe_emit(
    alert_type: str,
    server: str,
    fingerprint: str,
    severity: str,
    message: str,
    cooldown: timedelta,
    recovered: bool = False,
) -> list[dict[str, Any]]:
    if recovered or not message:
        return []
    previous = store.latest_alert(alert_type, server, fingerprint)
    now = datetime.now(JAKARTA)
    if previous:
        until = previous.get("cooldown_until") or ""
        try:
            until_dt = datetime.fromisoformat(until)
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=JAKARTA)
            if now < until_dt:
                return []
        except ValueError:
            pass
    cooldown_until = (now + cooldown).isoformat(timespec="seconds")
    store.insert_alert(alert_type, server, fingerprint, severity, message, cooldown_until)
    return [{"type": alert_type, "server": server, "severity": severity, "message": message}]
