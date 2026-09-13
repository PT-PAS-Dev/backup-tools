from __future__ import annotations

import base64
import logging
import re
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from monitor import store, sync_cache
from monitor.config import Node, Topology, load_topology, replication_credentials
from monitor.mariadb import MariaError, SafetyError, application_databases, collect, execute_clone, query_one
from monitor.ssh import run as ssh_run, ssh_configured
from monitor.status import format_size, row_sync_percent, rows_match, sizes_match

log = logging.getLogger("clone-monitor")
_job_lock = threading.Lock()

STREAM_FILTER = r"""
import os, sys, subprocess, shlex
header_path = os.environ["CLONE_HEADER"]
cnf = os.environ["CLONE_MYSQL_CNF"]
shell_import = os.environ.get("CLONE_MYSQL_SHELL", "").strip()
if shell_import:
    proc = subprocess.Popen(shell_import, shell=True, stdin=subprocess.PIPE)
else:
    mysql_bin = os.environ.get("CLONE_MYSQL_BIN", "mysql")
    cmd = [mysql_bin, "--defaults-extra-file=" + cnf]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
assert proc.stdin is not None
saved = []
while len(saved) < 150:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    saved.append(line)
    proc.stdin.write(line)
open(header_path, "wb").writelines(saved)
while True:
    chunk = sys.stdin.buffer.read(1024 * 1024)
    if not chunk:
        break
    proc.stdin.write(chunk)
proc.stdin.close()
raise SystemExit(proc.wait())
"""


class CloneError(Exception):
    pass


def _assert_clone_target(topology: Topology, target: Node) -> None:
    if target.role != "clone":
        raise SafetyError(f"{target.name} is not a clone target")
    if topology.is_source_host(target.host):
        raise SafetyError("Refusing to clone onto the production source host")
    if topology.source and target.name == topology.source.name:
        raise SafetyError("Refusing to treat the source as a clone")


def _assert_confirmation(target: Node, typed: str) -> None:
    expected = {target.host, target.name, target.display_host.lstrip(".")}
    if typed.strip() not in expected and typed.strip() != target.host:
        raise CloneError(
            f"Confirmation did not match. Type {target.host} (or {target.name}) to continue."
        )


def _database_catalog(src_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in src_payload.get("databases") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        size = int(item.get("size_bytes") or 0)
        rows.append(
            {
                "name": name,
                "size_bytes": size,
                "size_human": format_size(size),
                "table_count": int(item.get("table_count") or 0),
                "approx_rows": int(item.get("approx_rows") or 0),
            }
        )
    rows.sort(key=lambda row: row["size_bytes"], reverse=True)
    return rows


def _resolve_database_selection(
    src_payload: dict[str, Any], selected: list[str] | None
) -> tuple[list[str], int]:
    allowed = application_databases(src_payload)
    catalog = {row["name"]: row for row in _database_catalog(src_payload)}
    if selected is None:
        total = int(src_payload.get("size_bytes") or 0)
        return allowed, total
    if not selected:
        raise CloneError("Pilih minimal satu database untuk di-clone")
    names: list[str] = []
    seen: set[str] = set()
    for raw in selected:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        if name not in allowed:
            raise CloneError(f"Database {name!r} is not on the source or is a system schema")
        seen.add(name)
        names.append(name)
    if not names:
        raise CloneError("Pilih minimal satu database untuk di-clone")
    total = sum(int(catalog.get(name, {}).get("size_bytes") or 0) for name in names)
    return names, total


def _source_payload_cached(source: Node) -> dict[str, Any]:
    snap = store.latest_snapshots().get(source.name)
    if snap and snap.get("online") and snap.get("databases"):
        return snap
    return collect(source)


def _clone_sync_label(status: str) -> str:
    return {
        "synced": "Sudah sync",
        "partial": "Sebagian",
        "missing": "Belum ada",
    }.get(status, status)


def _annotate_catalog_for_target(catalog: list[dict[str, Any]], target: Node) -> list[dict[str, Any]]:
    snap = store.latest_snapshots().get(target.name) or {}
    dst_map = {str(d.get("name") or ""): d for d in snap.get("databases") or []}
    rows: list[dict[str, Any]] = []
    for item in catalog:
        name = item["name"]
        dst = dst_map.get(name)
        src_size = int(item.get("size_bytes") or 0)
        src_tables = int(item.get("table_count") or 0)
        src_rows = int(item.get("approx_rows") or 0)
        if not dst:
            status = "missing"
            synced = False
            clone_size = 0
            clone_tables = 0
            clone_rows = 0
            pct = 0
        else:
            clone_size = int(dst.get("size_bytes") or 0)
            clone_tables = int(dst.get("table_count") or 0)
            clone_rows = int(dst.get("approx_rows") or 0)
            size_ok = sizes_match(src_size, clone_size)
            tables_ok = src_tables == clone_tables
            rows_ok = rows_match(src_rows, clone_rows)
            synced = size_ok and tables_ok and rows_ok
            if synced:
                status = "synced"
            elif clone_size > 0 or clone_tables > 0:
                status = "partial"
            else:
                status = "missing"
            pct = row_sync_percent(src_rows, clone_rows) or 0
        rows.append(
            {
                **item,
                "sync_status": status,
                "sync_label": _clone_sync_label(status),
                "synced": synced,
                "clone_size_bytes": clone_size,
                "clone_size_human": format_size(clone_size),
                "clone_table_count": clone_tables,
                "clone_rows": clone_rows,
                "row_sync_percent": pct,
            }
        )
    return rows


def list_databases(target_name: str, topology: Topology | None = None) -> dict[str, Any]:
    topology = topology or load_topology()
    target = topology.by_name(target_name)
    if not target:
        raise CloneError(f"Unknown target {target_name}")
    source = topology.upstream(target)
    if not source:
        raise CloneError(f"{target.name} has no replicate_from source")
    src_payload = _source_payload_cached(source)
    if not src_payload.get("online"):
        raise CloneError(src_payload.get("error") or "Source is offline")
    catalog = _annotate_catalog_for_target(_database_catalog(src_payload), target)
    synced_count = sum(1 for row in catalog if row.get("synced"))
    return {
        "target": target.name,
        "target_host": target.host,
        "source": source.name,
        "source_host": source.host,
        "databases": catalog,
        "synced_count": synced_count,
        "total_size_bytes": int(src_payload.get("size_bytes") or 0),
        "total_size_human": format_size(int(src_payload.get("size_bytes") or 0)),
    }


def precheck(
    target_name: str,
    topology: Topology | None = None,
    selected_databases: list[str] | None = None,
) -> dict[str, Any]:
    topology = topology or load_topology()
    target = topology.by_name(target_name)
    if not target:
        raise CloneError(f"Unknown target {target_name}")
    _assert_clone_target(topology, target)
    source = topology.upstream(target)
    if not source:
        raise CloneError(f"{target.name} has no replicate_from source")

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("Target is clone", target.role == "clone", f"{target.name} role={target.role}")
    add("Target is not source IP", not topology.is_source_host(target.host), f"{target.host}")
    add("Source is not replica-of-target", True, f"{source.name} remains production source")

    with ThreadPoolExecutor(max_workers=2) as pool:
        src_future = pool.submit(collect, source)
        dst_future = pool.submit(collect, target)
        src_payload = src_future.result()
        dst_payload = dst_future.result()
    add("Source MariaDB reachable", bool(src_payload.get("online")), src_payload.get("error") or src_payload.get("version") or "")
    add("Clone MariaDB reachable", bool(dst_payload.get("online")), dst_payload.get("error") or dst_payload.get("version") or "")

    src_ver = str(src_payload.get("version") or "")
    dst_ver = str(dst_payload.get("version") or "")
    add("Source version 10.4.x", "10.4" in src_ver, src_ver or "unknown")
    add("Clone version 10.4.x", "10.4" in dst_ver, dst_ver or "unknown")

    catalog = _database_catalog(src_payload)
    try:
        chosen, size = _resolve_database_selection(src_payload, selected_databases)
    except CloneError as exc:
        chosen, size = [], 0
        add("Database selection", False, str(exc))
    else:
        add(
            "Database selection",
            bool(chosen),
            f"{len(chosen)} database(s) · {format_size(size)}",
        )
    add("Source size readable", size > 0 or bool(catalog), format_size(size) if size else format_size(int(src_payload.get("size_bytes") or 0)))

    ssh_ok = ssh_configured(target)
    add("SSH on clone (required to dump)", ssh_ok, target.ssh_user or "not set")

    docker_name = (target.docker_container or "").strip()
    if ssh_ok and docker_name:
        docker_detail = docker_name
        docker_ok = False
        try:
            code, out, err = ssh_run(
                target,
                _docker_cmd(target, f"inspect -f '{{{{.State.Running}}}}' {shlex.quote(docker_name)}"),
                timeout=20,
            )
            docker_ok = code == 0 and out.strip() == "true"
            if code != 0:
                docker_detail = (err or out or f"exit {code}")[:200]
            elif not docker_ok:
                docker_detail = f"{docker_name} not running"
        except Exception as exc:  # noqa: BLE001
            docker_detail = str(exc)[:200]
        add(
            "Docker container (clone dump)",
            docker_ok,
            docker_detail if docker_ok else f"{docker_detail} — cek docker_sudo / grup docker",
        )

    disk_ok = False
    disk_detail = "SSH required for disk check"
    if ssh_ok:
        try:
            code, out, err = ssh_run(target, "df -PB1 / | tail -n 1", timeout=15)
            parts = out.split()
            free = int(parts[3]) if len(parts) >= 4 else 0
            need = int(max(size, 1) * 1.3) if size else 0
            disk_ok = free >= need
            disk_detail = f"free {format_size(free)} / need ~{format_size(need)}"
            if code != 0:
                disk_ok = False
                disk_detail = err or out or f"df exit {code}"
        except Exception as exc:  # noqa: BLE001
            disk_detail = str(exc)
    add("Clone disk space", disk_ok, disk_detail)

    log_bin = str((dst_payload.get("variables") or {}).get("log_bin") or "").lower()
    log_slave = str((dst_payload.get("variables") or {}).get("log_slave_updates") or "").lower()
    downstream = any(node.replicate_from == target.name for node in topology.clones)
    chain_ok = True
    if downstream:
        chain_ok = log_bin in {"on", "1", "true"} and log_slave in {"on", "1", "true"}
        add(
            "Chain-ready (log_bin + log_slave_updates)",
            chain_ok,
            f"log_bin={log_bin or '?'} log_slave_updates={log_slave or '?'} — diperlukan nanti untuk replikasi ke downstream (mis. .31), bukan untuk clone awal ke clone pertama di chain (mis. .30).",
        )

    repl_user, repl_password = replication_credentials()
    repl_configured = bool(repl_user and repl_password)
    add(
        "Replication user configured",
        repl_configured,
        "CLONE_REPL_USER / CLONE_REPL_PASSWORD (atau MONITOR_REPL_*) — tanpa ini dump/restore tetap jalan, START SLAVE dilewati.",
    )

    soft_checks = {
        "Chain-ready (log_bin + log_slave_updates)",
        "Replication user configured",
    }
    blocking = [item for item in checks if not item["ok"] and item["name"] not in soft_checks]
    warnings = [item for item in checks if not item["ok"] and item["name"] in soft_checks]
    dst_app = set(application_databases(dst_payload))
    replace_needed = bool(chosen and dst_app.intersection(chosen))
    return {
        "ok": not blocking,
        "can_replicate": repl_configured,
        "warnings": [item["name"] for item in warnings],
        "target": target.name,
        "source": source.name,
        "source_host": source.host,
        "target_host": target.host,
        "source_size": size,
        "source_size_human": format_size(size),
        "source_total_size_human": format_size(int(src_payload.get("size_bytes") or 0)),
        "databases": chosen if chosen else application_databases(src_payload),
        "database_catalog": catalog,
        "selected_count": len(chosen),
        "checks": checks,
        "blocking": [item["name"] for item in blocking],
        "ssh_ok": ssh_ok,
        "replace_needed": replace_needed,
        "replace_databases": sorted(dst_app.intersection(chosen)) if chosen else [],
    }


def start_job(
    target_name: str,
    typed_confirm: str,
    replace: bool,
    ip: str,
    topology: Topology | None = None,
    selected_databases: list[str] | None = None,
) -> dict[str, Any]:
    topology = topology or load_topology()
    target = topology.by_name(target_name)
    if not target:
        raise CloneError(f"Unknown target {target_name}")
    _assert_clone_target(topology, target)
    _assert_confirmation(target, typed_confirm)
    if store.active_jobs():
        raise CloneError("A clone job is already running")

    check = precheck(target_name, topology, selected_databases=selected_databases)
    if not check["ok"]:
        raise CloneError("Pre-check failed: " + ", ".join(check["blocking"]))
    databases = check["databases"]
    if not databases:
        raise CloneError("Pilih minimal satu database untuk di-clone")
    if check["replace_needed"] and not replace:
        names = ", ".join(check.get("replace_databases") or check["databases"][:5])
        raise CloneError(
            f"Database sudah ada di clone: {names}. Centang «Ganti database aplikasi…» lalu start lagi (hanya di host clone)."
        )

    source = topology.upstream(target)
    assert source is not None
    job_id = store.create_job(
        source.name,
        target.name,
        int(check["source_size"]),
        f"queued · {len(databases)} db: {', '.join(databases[:5])}{'…' if len(databases) > 5 else ''}",
    )
    store.insert_audit(
        "admin",
        "CLONE_START",
        target.name,
        "accepted",
        f"from {source.name} · dbs={','.join(databases)}",
        ip,
    )
    skip_replication = not check.get("can_replicate")
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, topology, source, target, databases, replace, skip_replication),
        daemon=True,
        name=f"clone-{target.name}",
    )
    thread.start()
    return {"job_id": job_id, "status": "RUNNING"}


def _run_job(
    job_id: int,
    topology: Topology,
    source: Node,
    target: Node,
    databases: list[str],
    replace: bool,
    skip_replication: bool = False,
) -> None:
    if not _job_lock.acquire(blocking=False):
        store.update_job(job_id, status="FAILED", phase="PRECHECK", message="Another clone job holds the lock")
        return
    try:
        _assert_clone_target(topology, target)
        store.append_job_log(job_id, f"Start clone {source.host} -> {target.host}")
        store.update_job(job_id, phase="PREPARE", message="preparing clone host")
        if replace:
            _drop_application_databases(target, job_id, only=databases)
        _dump_restore(job_id, source, target, databases)
        if skip_replication:
            store.append_job_log(
                job_id,
                "Replication skipped: set CLONE_REPL_USER / CLONE_REPL_PASSWORD di .env lalu jalankan CHANGE MASTER + START SLAVE manual atau start clone lagi.",
            )
            store.update_job(
                job_id,
                status="COMPLETED",
                phase="DONE",
                message="Data copied. Replikasi tidak dijalankan (user repl belum dikonfigurasi).",
            )
        else:
            store.update_job(job_id, phase="CONFIGURE_REPLICATION", message="configuring replication on clone")
            _configure_replication(job_id, source, target)
            store.update_job(job_id, phase="POSTCHECK", message="checking replica threads")
            _postcheck(job_id, target)
            store.update_job(
                job_id,
                status="COMPLETED",
                phase="DONE",
                message="Initial clone completed. Replication started on clone only.",
            )
        store.append_job_log(job_id, "Completed. Source was not modified.")
        store.insert_audit("admin", "CLONE_FINISH", target.name, "ok", "completed", "")
    except Exception as exc:  # noqa: BLE001
        log.exception("clone job %s failed", job_id)
        store.update_job(job_id, status="FAILED", message=str(exc)[:500])
        store.append_job_log(job_id, f"FAILED: {exc}")
        store.insert_audit("admin", "CLONE_FINISH", target.name, "failed", str(exc)[:500], "")
    finally:
        _job_lock.release()


def _drop_application_databases(target: Node, job_id: int, only: list[str] | None = None) -> None:
    _assert_clone_role(target)
    payload = collect(target)
    names = application_databases(payload)
    if only is not None:
        allow = set(only)
        names = [name for name in names if name in allow]
    for name in names:
        store.append_job_log(job_id, f"DROP DATABASE on clone only: {name}")
        execute_clone(target, f"DROP DATABASE IF EXISTS `{_ident(name)}`")


def _ident(name: str) -> str:
    if not name or any(ch in name for ch in ("`", ";", "/", "\\", " ", "\n")):
        raise SafetyError(f"Refusing unsafe identifier {name!r}")
    return name


def _assert_clone_role(node: Node) -> None:
    if node.role != "clone":
        raise SafetyError(f"Refusing destructive SQL on {node.name}")


def _write_remote_cnf(target: Node, contents: str, path: str) -> None:
    encoded = contents.replace("'", "'\"'\"'")
    command = f"umask 077; printf '%s' '{encoded}' > {shlex.quote(path)}"
    code, _out, err = ssh_run(target, command, timeout=15)
    if code != 0:
        raise CloneError(f"Cannot write {path}: {err}")


def _sudo_password_b64(target: Node) -> str:
    pw = (target.ssh_sudo_password or target.ssh_password or "").strip()
    if not pw:
        return ""
    return base64.b64encode(pw.encode()).decode()


def _docker_cmd(target: Node, docker_args: str) -> str:
    if not target.docker_sudo:
        return f"docker {docker_args}"
    b64 = _sudo_password_b64(target)
    if b64:
        return f"echo {shlex.quote(b64)} | base64 -d | sudo -S -p '' docker {docker_args}"
    return f"sudo -n docker {docker_args}"


def _write_container_cnf(target: Node, container: str, contents: str, path: str) -> None:
    encoded = contents.replace("'", "'\"'\"'")
    inner = f"umask 077; printf '%s' '{encoded}' > {shlex.quote(path)}"
    command = _docker_cmd(target, f"exec {shlex.quote(container)} sh -c {shlex.quote(inner)}")
    code, _out, err = ssh_run(target, command, timeout=15)
    if code != 0:
        raise CloneError(f"Cannot write {path} in container {container}: {err}")


def _dump_restore(job_id: int, source: Node, target: Node, databases: list[str]) -> None:
    if not databases:
        raise CloneError("Source has no application databases to clone")
    store.update_job(job_id, phase="DUMP_RESTORE", current_database=",".join(databases), message="streaming dump on clone host")
    store.append_job_log(job_id, f"Dumping {len(databases)} database(s) via SSH on {target.host}")

    src_cnf = "/tmp/clone-monitor-src.cnf"
    dst_cnf = "/tmp/clone-monitor-dst.cnf"
    header = "/tmp/clone-monitor.header"
    db_args = " ".join(shlex.quote(name) for name in databases)
    dump_flags = (
        "--single-transaction --quick --routines --triggers --events --hex-blob "
        "--default-character-set=utf8mb4 --master-data=2 --databases "
    )
    encoded = base64.b64encode(STREAM_FILTER.encode()).decode("ascii")
    py_pipe = f'python3 -c "import base64; exec(base64.b64decode(\'{encoded}\').decode())"'

    container = (target.docker_container or "").strip()
    if container:
        c = shlex.quote(container)
        dst_cnf_container = "/tmp/clone-monitor-dst.cnf"
        store.append_job_log(
            job_id,
            f"Dump + restore via docker container {container} (mariadb-dump | mariadb di dalam container)",
        )
        _write_container_cnf(
            target,
            container,
            f"[client]\nhost={source.host}\nport={source.port}\nuser={source.user}\npassword={source.password}\n",
            src_cnf,
        )
        _write_container_cnf(
            target,
            container,
            f"[client]\nuser={target.user}\npassword={target.password}\n",
            dst_cnf_container,
        )
        inspect = _docker_cmd(target, f"inspect -f '{{{{.State.Running}}}}' {c}")
        dump = _docker_cmd(
            target,
            f"exec {c} mariadb-dump --defaults-extra-file={shlex.quote(src_cnf)} {dump_flags}{db_args}",
        )
        import_shell = _docker_cmd(
            target,
            f"exec -i {c} mariadb --defaults-extra-file={shlex.quote(dst_cnf_container)}",
        )
        rm_cnf = _docker_cmd(
            target,
            f"exec {c} rm -f {shlex.quote(src_cnf)} {shlex.quote(dst_cnf_container)}",
        )
        remote = (
            "set -o pipefail; "
            f"{inspect} | grep -qx true || "
            f'{{ echo "Container {container} tidak jalan" >&2; exit 1; }}; '
            f"export CLONE_HEADER={shlex.quote(header)} CLONE_MYSQL_CNF={shlex.quote(dst_cnf_container)} "
            f"CLONE_MYSQL_SHELL={shlex.quote(import_shell)}; "
            f"{dump} | {py_pipe}; "
            f"ec=$?; {rm_cnf}; exit $ec"
        )
    else:
        _write_remote_cnf(
            target,
            f"[client]\nhost={source.host}\nport={source.port}\nuser={source.user}\npassword={source.password}\n",
            src_cnf,
        )
        local_mysql_host = (target.local_mysql_host or "127.0.0.1").strip()
        _write_remote_cnf(
            target,
            f"[client]\nhost={local_mysql_host}\nuser={target.user}\npassword={target.password}\nport={target.port}\n",
            dst_cnf,
        )
        store.append_job_log(job_id, f"Restore target MySQL: {local_mysql_host}:{target.port} (TCP, bukan socket)")
        remote = (
            "set -o pipefail; "
            'DUMP_BIN="$(command -v mariadb-dump 2>/dev/null || command -v mysqldump 2>/dev/null || true)"; '
            'MYSQL_BIN="$(command -v mariadb 2>/dev/null || command -v mysql 2>/dev/null || true)"; '
            'if [ -z "$DUMP_BIN" ] || [ -z "$MYSQL_BIN" ]; then '
            'echo "Pasang mariadb-client di host clone, atau set docker_container di config.yaml" >&2; exit 127; fi; '
            f'export CLONE_HEADER={shlex.quote(header)} CLONE_MYSQL_CNF={shlex.quote(dst_cnf)} CLONE_MYSQL_BIN="$MYSQL_BIN"; '
            f'"$DUMP_BIN" --defaults-extra-file={shlex.quote(src_cnf)} {dump_flags}{db_args} '
            f"| {py_pipe}; "
            f"ec=$?; rm -f {shlex.quote(src_cnf)} {shlex.quote(dst_cnf)}; exit $ec"
        )

    stop = threading.Event()
    progress = threading.Thread(target=_watch_progress, args=(job_id, source, target, stop), daemon=True)
    progress.start()
    try:
        code, out, err = ssh_run(target, remote, timeout=60 * 60 * 12)
        if code != 0:
            raise CloneError((err or out or f"dump exit {code}")[:1000])
        store.append_job_log(job_id, "Dump/restore stream finished")
        _store_header(job_id, target, header)
    finally:
        stop.set()
        progress.join(timeout=5)
        try:
            if container:
                ssh_run(
                    target,
                    _docker_cmd(
                        target,
                        f"exec {shlex.quote(container)} rm -f {shlex.quote(src_cnf)} {shlex.quote('/tmp/clone-monitor-dst.cnf')}",
                    ),
                    timeout=10,
                )
            else:
                ssh_run(target, f"rm -f {shlex.quote(src_cnf)} {shlex.quote(dst_cnf)}", timeout=10)
        except Exception:
            pass


def _store_header(job_id: int, target: Node, header: str) -> None:
    try:
        code, out, err = ssh_run(target, f"cat {shlex.quote(header)}", timeout=15)
        if code == 0 and out.strip():
            store.append_job_log(job_id, "Captured dump header for replication coordinates")
            store.update_job(job_id, message="dump header captured")
            # Keep header remotely for configure step; path is fixed.
            return
        store.append_job_log(job_id, f"Dump header missing: {err or 'empty'}")
    except Exception as exc:  # noqa: BLE001
        store.append_job_log(job_id, f"Dump header read failed: {exc}")


def _watch_progress(job_id: int, source: Node, target: Node, stop: threading.Event) -> None:
    source_size = 0
    try:
        snap = store.latest_snapshots().get(source.name) or collect(source)
        source_size = int(snap.get("size_bytes") or 0)
        store.update_job(job_id, total_bytes=source_size)
    except Exception:
        pass
    while not stop.wait(20):
        try:
            snap = store.latest_snapshots().get(target.name)
            if not snap or not snap.get("online"):
                dst = collect(target)
            else:
                dst = snap
            copied = int(dst.get("size_bytes") or 0)
            current = ""
            dbs = dst.get("databases") or []
            if dbs:
                current = dbs[0]["name"]
            store.update_job(job_id, copied_bytes=copied, current_database=current, total_bytes=source_size)
        except Exception:
            continue


def _parse_header(text: str) -> dict[str, str]:
    result = {"gtid": "", "log_file": "", "log_pos": ""}
    for line in text.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if "gtid_slave_pos" in stripped.lower() or "gtid_purged" in stripped.lower():
            if "'" in stripped:
                result["gtid"] = stripped.split("'")[1]
            elif '"' in stripped:
                result["gtid"] = stripped.split('"')[1]
        if "MASTER_LOG_FILE" in stripped.upper():
            for part in stripped.replace(";", " ").split(","):
                part = part.strip()
                if "MASTER_LOG_FILE" in part.upper() and "=" in part:
                    result["log_file"] = part.split("=", 1)[1].strip().strip("'\"")
                if "MASTER_LOG_POS" in part.upper() and "=" in part:
                    result["log_pos"] = part.split("=", 1)[1].strip().strip("'\"")
    return result


def _configure_replication(job_id: int, source: Node, target: Node) -> None:
    _assert_clone_role(target)
    if source.role == "clone" and source.host == target.host:
        raise SafetyError("Source and target host are identical")
    repl_user, repl_password = replication_credentials()
    if not repl_user or not repl_password:
        raise CloneError("CLONE_REPL_USER / CLONE_REPL_PASSWORD belum di-set di .env")

    code, header, err = ssh_run(target, "cat /tmp/clone-monitor.header", timeout=15)
    coords = _parse_header(header if code == 0 else "")
    if not coords.get("gtid") and not coords.get("log_file"):
        store.append_job_log(job_id, "Header coordinates missing; reading source GTID as fallback")
        row = query_one(source, "SELECT @@gtid_binlog_pos AS pos")
        coords["gtid"] = str((row or {}).get("pos") or "")

    try:
        execute_clone(target, "STOP SLAVE")
    except MariaError as exc:
        store.append_job_log(job_id, f"STOP SLAVE (ok if not yet a replica): {exc}")
    if coords.get("gtid"):
        gtid = coords["gtid"].replace("'", "")
        if not re.fullmatch(r"[0-9A-Za-z:,._-]+", gtid):
            raise SafetyError(f"Refusing unsafe GTID value: {gtid!r}")
        execute_clone(target, f"SET GLOBAL gtid_slave_pos='{gtid}'")
        store.append_job_log(job_id, f"gtid_slave_pos={gtid}")

    # CHANGE MASTER only on clone. MASTER_HOST is the upstream, never the clone itself.
    change = (
        "CHANGE MASTER TO "
        f"MASTER_HOST='{_escape(source.host)}', "
        f"MASTER_PORT={int(source.port)}, "
        f"MASTER_USER='{_escape(repl_user)}', "
        f"MASTER_PASSWORD='{_escape(repl_password)}', "
    )
    if coords.get("gtid"):
        change += "MASTER_USE_GTID=slave_pos"
    elif coords.get("log_file") and coords.get("log_pos"):
        change += (
            f"MASTER_LOG_FILE='{_escape(coords['log_file'])}', "
            f"MASTER_LOG_POS={int(coords['log_pos'])}"
        )
    else:
        raise CloneError("No GTID or binlog coordinates available to start replication")
    execute_clone(target, change)
    execute_clone(target, "START SLAVE")
    store.append_job_log(job_id, f"START SLAVE on {target.name} only; upstream={source.host}")
    time.sleep(2)


def _postcheck(job_id: int, target: Node) -> None:
    payload = collect(target)
    slave = payload.get("slave") if isinstance(payload.get("slave"), dict) else None
    if not slave:
        raise CloneError("START SLAVE issued but SHOW SLAVE STATUS is empty")
    io_running = slave.get("io_running")
    sql_running = slave.get("sql_running")
    store.append_job_log(job_id, f"IO={io_running} SQL={sql_running} lag={slave.get('seconds_behind')}")
    if slave.get("last_error"):
        raise CloneError(f"Replication error after start: {slave.get('last_error')}")
    if io_running not in {"Yes", "Connecting"}:
        raise CloneError(f"IO thread is {io_running}")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def progress_payload(topology: Topology | None = None, *, include_sync: bool = True) -> dict[str, Any]:
    topology = topology or load_topology()
    jobs = store.list_jobs(30)
    latest = store.latest_job()
    active = store.active_jobs()
    for job in jobs:
        total = int(job.get("total_bytes") or 0)
        copied = int(job.get("copied_bytes") or 0)
        reliable = total > 0 and copied > 0 and job.get("status") == "RUNNING"
        job["progress_reliable"] = reliable
        job["percent"] = int(min(99, copied * 100 / total)) if reliable else None
        job["copied_human"] = format_size(copied)
        job["total_human"] = format_size(total) if total else "N/A"
        job["remaining_human"] = format_size(max(total - copied, 0)) if total and copied else "N/A"
        if job.get("status") == "RUNNING" and not reliable:
            phase = str(job.get("phase") or "")
            job["phase_label"] = {
                "DUMP_RESTORE": "Mengalirkan dump → restore",
                "CONFIGURE_REPLICATION": "Mengatur replikasi",
                "POSTCHECK": "Memeriksa slave",
                "PREPARE": "Menyiapkan host clone",
            }.get(phase, phase or "Berjalan")
    sync_clones = sync_cache.sync_clones(topology) if include_sync else []
    return {
        "jobs": jobs,
        "latest": jobs[0] if jobs else latest,
        "active_jobs": active,
        "sync_clones": sync_clones,
        "clones": [{"name": node.name, "host": node.host, "from": node.replicate_from} for node in topology.clones],
        "source": {"name": topology.source.name, "host": topology.source.host} if topology.source else None,
    }


def cloning_targets(active: list[dict[str, Any]]) -> set[str]:
    return {str(job.get("target") or "") for job in active}
