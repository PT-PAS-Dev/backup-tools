#!/usr/bin/env python3
"""Dump each MySQL table to its own SQL file and upload to Google Drive."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
import pymysql
from pymysql.cursors import SSCursor
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from drive_auth import AuthError, load_credentials
from paths import PROJECT_ROOT, load_project_env

load_project_env()

JAKARTA = ZoneInfo("Asia/Jakarta")
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DATE_FOLDER_RE = re.compile(r"^\d{8}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backup")
_log_dir = PROJECT_ROOT / "logs"
_log_dir.mkdir(exist_ok=True)
_file = logging.FileHandler(_log_dir / "backup.out.log")
_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
log.addHandler(_file)


@dataclass
class DatabaseTarget:
    name: str
    folder: str


@dataclass
class Connection:
    name: str
    host: str
    port: int
    user: str
    password: str
    databases: list[DatabaseTarget]


class BackupError(Exception):
    pass


@dataclass
class TableLog:
    connection: str
    database: str
    folder: str
    table: str
    status: str
    size_bytes: int = 0
    duration_s: float = 0.0
    message: str = ""


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{size_bytes}B"


def write_backup_log(path: Path, date_folder: str, started: datetime, entries: list[TableLog]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = sum(1 for item in entries if item.status == "OK")
    skipped = sum(1 for item in entries if item.status == "SKIP")
    failed = sum(1 for item in entries if item.status == "FAIL")
    lines = [
        f"Backup {date_folder} Asia/Jakarta",
        f"Started : {started.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Finished: {datetime.now(JAKARTA).strftime('%Y-%m-%d %H:%M:%S')}",
        f"Tables  : {len(entries)} (DUMP={ok} SKIP={skipped} FAIL={failed})",
        "",
        f"{'status':<6} {'duration':>8} {'size':>10}  table",
        "-" * 72,
    ]
    for item in entries:
        target = f"{item.connection}/{item.database}.{item.table}"
        note = f"  {item.message}" if item.message else ""
        lines.append(
            f"{item.status:<6} {item.duration_s:7.1f}s {format_size(item.size_bytes):>10}  {target}{note}"
        )
    path.write_text("\n".join(lines) + "\n")


def _connection_env_suffix(connection_name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in connection_name).upper()


def env_name(connection_name: str) -> str:
    return f"MYSQL_PASSWORD_{_connection_env_suffix(connection_name)}"


def env_user_name(connection_name: str) -> str:
    return f"MYSQL_USER_{_connection_env_suffix(connection_name)}"


def parse_databases(raw: Any, connection_name: str) -> list[DatabaseTarget]:
    if not raw:
        raise BackupError(f"Connection {connection_name!r} has no databases")

    targets: list[DatabaseTarget] = []
    for item in raw:
        if isinstance(item, str):
            targets.append(DatabaseTarget(name=item, folder=item))
            continue
        if isinstance(item, dict):
            name = item.get("name")
            if not name:
                raise BackupError(f"Database entry in {connection_name!r} is missing name")
            targets.append(DatabaseTarget(name=name, folder=item.get("folder") or name))
            continue
        raise BackupError(f"Invalid database entry in {connection_name!r}: {item!r}")
    return targets


def load_connections(config_path: Path) -> list[Connection]:
    try:
        if not config_path.is_file():
            raise BackupError(
                f"Config file not found: {config_path}. "
                "Run: ./scripts/load-secrets.sh"
            )
        raw_config = config_path.read_text()
    except OSError as exc:
        raise BackupError(
            f"Cannot read config file {config_path}: {exc}. "
            "Run: ./scripts/load-secrets.sh"
        ) from exc

    data = yaml.safe_load(raw_config) or {}
    raw_connections = data.get("connections")
    if not raw_connections:
        raise BackupError("config.yaml must contain a non-empty connections list")

    connections: list[Connection] = []
    for raw in raw_connections:
        name = str(raw.get("name") or raw.get("host") or "default")
        host = raw.get("host")
        user = (os.environ.get(env_user_name(name)) or str(raw.get("user") or "")).strip()
        if not host:
            raise BackupError(f"Connection {name!r} needs host")
        if not user:
            raise BackupError(
                f"Connection {name!r} needs user in config or env {env_user_name(name)!r}"
            )

        password = os.environ.get(env_name(name))
        if password is None:
            password = str(raw.get("password") or "")

        connections.append(
            Connection(
                name=name,
                host=str(host),
                port=int(raw.get("port") or 3306),
                user=str(user),
                password=password,
                databases=parse_databases(raw.get("databases"), name),
            )
        )
    return connections


def configured_databases(connections: list[Connection]) -> list[str]:
    names: list[str] = []
    for connection in connections:
        for database in connection.databases:
            if database.name not in names:
                names.append(database.name)
    return names


def filter_connections(connections: list[Connection], selected: list[str]) -> list[Connection]:
    wanted = set(selected)
    known = set(configured_databases(connections))
    missing = sorted(wanted - known)
    if missing:
        raise BackupError(
            "Database tidak ada di config.yaml: "
            + ", ".join(missing)
            + ". Yang tersedia: "
            + ", ".join(configured_databases(connections))
        )
    filtered: list[Connection] = []
    for connection in connections:
        databases = [item for item in connection.databases if item.name in wanted]
        if databases:
            filtered.append(replace(connection, databases=databases))
    if not filtered:
        raise BackupError("Tidak ada koneksi yang punya database tersebut")
    return filtered


def pick_databases(names: list[str]) -> list[str]:
    print("Pilih database yang akan di-backup:")
    for index, name in enumerate(names, start=1):
        print(f"  {index}) {name}")
    print("  0) semua")
    raw = input("Nomor (boleh beberapa, pisah koma): ").strip()
    if not raw or raw == "0":
        return names
    chosen: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            index = int(part)
        except ValueError as exc:
            raise BackupError(f"Pilihan tidak valid: {part}") from exc
        if index < 1 or index > len(names):
            raise BackupError(f"Pilihan tidak valid: {part}")
        name = names[index - 1]
        if name not in chosen:
            chosen.append(name)
    if not chosen:
        raise BackupError("Tidak ada database yang dipilih")
    return chosen


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backup MySQL per tabel ke Google Drive")
    parser.add_argument(
        "-d",
        "--database",
        action="append",
        dest="databases",
        metavar="NAMA",
        help="Backup database ini saja (boleh diulang). Default: semua di config.yaml",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Tampilkan daftar database di config.yaml lalu keluar",
    )
    parser.add_argument(
        "--pick",
        action="store_true",
        help="Pilih database secara interaktif",
    )
    return parser.parse_args(argv)


def ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def open_mysql(connection: Connection, database: str | None = None) -> pymysql.connections.Connection:
    try:
        conn = pymysql.connect(
            host=connection.host,
            port=connection.port,
            user=connection.user,
            password=connection.password,
            database=database,
            charset="utf8mb4",
            ssl_disabled=True,
            autocommit=False,
        )
        conn.begin()
        return conn
    except pymysql.Error as exc:
        raise BackupError(str(exc)) from exc


def list_tables(conn: pymysql.connections.Connection) -> list[tuple[str, str]]:
    with conn.cursor() as cursor:
        cursor.execute("SHOW FULL TABLES")
        rows = cursor.fetchall()
    return [(str(name), str(kind)) for name, kind in rows]


def dump_table(
    conn: pymysql.connections.Connection,
    table: str,
    kind: str,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    quoted = ident(table)
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SHOW CREATE TABLE {quoted}")
            create_sql = str(cursor.fetchone()[1])
        drop = f"DROP VIEW IF EXISTS {quoted};" if kind.upper() == "VIEW" else f"DROP TABLE IF EXISTS {quoted};"
        with destination.open("w", encoding="utf-8") as handle:
            handle.write(f"-- {table}\nSET NAMES utf8mb4;\n{drop}\n{create_sql};\n\n")
            if kind.upper() == "VIEW":
                return
            with conn.cursor(SSCursor) as cursor:
                cursor.execute(f"SELECT * FROM {quoted}")
                columns = [ident(str(col[0])) for col in (cursor.description or [])]
                if not columns:
                    return
                col_sql = ", ".join(columns)
                batch: list[str] = []
                for row in cursor:
                    values = ", ".join("NULL" if item is None else conn.escape(item) for item in row)
                    batch.append(f"({values})")
                    if len(batch) >= 200:
                        handle.write(f"INSERT INTO {quoted} ({col_sql}) VALUES\n")
                        handle.write(",\n".join(batch))
                        handle.write(";\n")
                        batch.clear()
                if batch:
                    handle.write(f"INSERT INTO {quoted} ({col_sql}) VALUES\n")
                    handle.write(",\n".join(batch))
                    handle.write(";\n")
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise BackupError(f"Empty dump for {table}")
    except pymysql.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(str(exc)) from exc


def table_fingerprint(conn: pymysql.connections.Connection, table: str) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute("SHOW TABLE STATUS WHERE Name = %s", (table,))
        row = cursor.fetchone()
        columns = [str(col[0]).lower() for col in (cursor.description or [])]
    if not row:
        return {"name": table}
    data = dict(zip(columns, row))
    update_time = data.get("update_time")
    return {
        "engine": str(data.get("engine") or ""),
        "rows": int(data.get("rows") or 0),
        "data_length": int(data.get("data_length") or 0),
        "index_length": int(data.get("index_length") or 0),
        "auto_increment": data.get("auto_increment"),
        "checksum": data.get("checksum"),
        "update_time": (
            update_time.isoformat()
            if hasattr(update_time, "isoformat")
            else (str(update_time) if update_time else None)
        ),
    }


def fingerprint_fields(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: meta.get(key) for key in (
        "engine",
        "data_length",
        "index_length",
        "auto_increment",
        "checksum",
        "update_time",
    )}


def load_state(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return {}
        data = json.loads(path.read_text())
    except OSError as exc:
        log.warning("Cannot read state file %s (%s); starting empty", path, exc)
        return {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str))
    except OSError as exc:
        raise BackupError(
            f"Cannot write state file {path}: {exc}. "
            "On the host run: chmod -R a+rwX state"
        ) from exc


def escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveClient:
    def __init__(self, root_folder_id: str) -> None:
        if not root_folder_id:
            raise BackupError("GDRIVE_FOLDER_ID is required")
        try:
            creds = load_credentials(interactive=False)
        except AuthError as exc:
            raise BackupError(str(exc)) from exc
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.root_folder_id = root_folder_id

    def _list(self, query: str) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            files.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return files

    def find_in_parent(self, parent_id: str, name: str, folder: bool) -> str | None:
        mime = f"mimeType = '{DRIVE_FOLDER_MIME}'" if folder else f"mimeType != '{DRIVE_FOLDER_MIME}'"
        query = (
            f"name = '{escape_drive_query(name)}' and '{parent_id}' in parents "
            f"and {mime} and trashed = false"
        )
        files = self._list(query)
        return files[0]["id"] if files else None

    def ensure_folder(self, parent_id: str, name: str) -> str:
        existing = self.find_in_parent(parent_id, name, folder=True)
        if existing:
            return existing
        created = (
            self.service.files()
            .create(
                body={
                    "name": name,
                    "mimeType": DRIVE_FOLDER_MIME,
                    "parents": [parent_id],
                },
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return created["id"]

    def upload_file(self, parent_id: str, local_path: Path, mimetype: str = "application/sql") -> str:
        media = MediaFileUpload(str(local_path), mimetype=mimetype, resumable=True)
        existing = self.find_in_parent(parent_id, local_path.name, folder=False)
        if existing:
            self.service.files().update(
                fileId=existing,
                media_body=media,
                supportsAllDrives=True,
            ).execute()
            return existing
        created = (
            self.service.files()
            .create(
                body={"name": local_path.name, "parents": [parent_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        return created["id"]

    def copy_file(self, file_id: str, parent_id: str, name: str) -> str:
        existing = self.find_in_parent(parent_id, name, folder=False)
        copied = (
            self.service.files()
            .copy(
                fileId=file_id,
                body={"name": name, "parents": [parent_id]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        new_id = copied["id"]
        if existing and existing != new_id:
            self.service.files().delete(fileId=existing, supportsAllDrives=True).execute()
        return new_id

    def apply_retention(self, retention_days: int, today: datetime) -> None:
        if retention_days <= 0:
            return

        cutoff = today.date() - timedelta(days=retention_days)
        query = (
            f"'{self.root_folder_id}' in parents and mimeType = '{DRIVE_FOLDER_MIME}' "
            f"and trashed = false"
        )
        for folder in self._list(query):
            name = folder["name"]
            if not DATE_FOLDER_RE.match(name):
                continue
            try:
                folder_date = datetime.strptime(name, "%d%m%Y").date()
            except ValueError:
                continue
            if folder_date >= cutoff:
                continue
            log.info("Deleting expired Drive folder %s", name)
            self.service.files().delete(fileId=folder["id"], supportsAllDrives=True).execute()


def backup_connection(
    connection: Connection,
    date_folder: str,
    backup_root: Path,
    drive: DriveClient,
    table_logs: list[TableLog],
    state: dict[str, Any],
    incremental: bool,
) -> list[str]:
    errors: list[str] = []
    date_folder_id = drive.ensure_folder(drive.root_folder_id, date_folder)
    for database in connection.databases:
        label = f"{connection.name}/{database.name}"
        conn = None
        try:
            conn = open_mysql(connection, database.name)
            tables = list_tables(conn)
        except BackupError as exc:
            errors.append(f"{label}: {exc}")
            log.error("Gagal list tabel %s: %s", label, exc)
            table_logs.append(
                TableLog(
                    connection=connection.name,
                    database=database.name,
                    folder=database.folder,
                    table="*",
                    status="FAIL",
                    message=str(exc),
                )
            )
            if conn:
                conn.close()
            continue

        if not tables:
            log.warning("Tidak ada tabel di %s", label)
            conn.close()
            continue

        log.info("Database %s: %s tabel", label, len(tables))
        db_folder_id = drive.ensure_folder(date_folder_id, database.folder)

        for index, (table, kind) in enumerate(tables, start=1):
            prefix = f"[{index}/{len(tables)}]"
            sql_path = backup_root / date_folder / database.folder / f"{table}.sql"
            started = time.monotonic()
            entry = TableLog(
                connection=connection.name,
                database=database.name,
                folder=database.folder,
                table=table,
                status="FAIL",
            )
            state_key = f"{connection.name}/{database.name}/{table}"
            try:
                fingerprint = table_fingerprint(conn, table)
                previous = state.get(state_key) or {}
                unchanged = (
                    incremental
                    and fingerprint_fields(previous) == fingerprint_fields(fingerprint)
                    and previous.get("file_id")
                )
                if unchanged:
                    try:
                        file_id = drive.copy_file(previous["file_id"], db_folder_id, f"{table}.sql")
                        fingerprint["file_id"] = file_id
                        state[state_key] = fingerprint
                        entry.status = "SKIP"
                        entry.message = "tidak berubah, disalin dari backup sebelumnya"
                        entry.duration_s = time.monotonic() - started
                        log.info("%s SKIP %s.%s (tidak berubah)", prefix, label, table)
                        continue
                    except Exception as copy_exc:  # noqa: BLE001
                        log.warning(
                            "%s salin gagal %s.%s, dump ulang: %s",
                            prefix,
                            label,
                            table,
                            copy_exc,
                        )

                log.info("%s dump %s.%s", prefix, label, table)
                dump_table(conn, table, kind, sql_path)
                entry.size_bytes = sql_path.stat().st_size
                log.info(
                    "%s dump selesai %s.%s (%s)",
                    prefix,
                    label,
                    table,
                    format_size(entry.size_bytes),
                )
                file_id = drive.upload_file(db_folder_id, sql_path)
                fingerprint["file_id"] = file_id
                state[state_key] = fingerprint
                entry.status = "OK"
                entry.duration_s = time.monotonic() - started
                log.info(
                    "%s upload OK %s/%s/%s.sql (%s, %.1fs)",
                    prefix,
                    date_folder,
                    database.folder,
                    table,
                    format_size(entry.size_bytes),
                    entry.duration_s,
                )
            except Exception as exc:  # noqa: BLE001
                entry.duration_s = time.monotonic() - started
                entry.message = str(exc)
                errors.append(f"{label}.{table}: {exc}")
                log.error("%s GAGAL %s.%s (%.1fs): %s", prefix, label, table, entry.duration_s, exc)
            finally:
                table_logs.append(entry)
                sql_path.unlink(missing_ok=True)
        conn.close()

    return errors


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(JAKARTA)
    date_folder = now.strftime("%d%m%Y")
    config_path = Path(os.environ.get("CONFIG_PATH", str(PROJECT_ROOT / "config.yaml")))
    backup_dir = Path(os.environ.get("BACKUP_DIR", str(PROJECT_ROOT / "tmp")))
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    retention_days = int(os.environ.get("RETENTION_DAYS", "30"))
    incremental = os.environ.get("INCREMENTAL", "1").strip() not in {"0", "false", "False"}
    state_path = Path(os.environ.get("STATE_PATH", str(PROJECT_ROOT / "state" / "fingerprints.json")))
    state = load_state(state_path)

    try:
        connections = load_connections(config_path)
        available = configured_databases(connections)
        if args.list:
            print("Database di config.yaml:")
            for name in available:
                print(f"  {name}")
            return 0
        if args.pick:
            selected = pick_databases(available)
            connections = filter_connections(connections, selected)
        elif args.databases:
            connections = filter_connections(connections, args.databases)
            selected = args.databases
        else:
            selected = available
    except BackupError as exc:
        log.error("%s", exc)
        return 1

    log.info(
        "Mulai backup %s (Asia/Jakarta) mode=%s database=%s",
        date_folder,
        "incremental" if incremental else "full",
        ", ".join(selected),
    )
    errors: list[str] = []
    table_logs: list[TableLog] = []
    dated = backup_dir / date_folder
    drive: DriveClient | None = None

    try:
        drive = DriveClient(folder_id)
    except BackupError as exc:
        log.error("%s", exc)
        return 1

    try:
        for connection in connections:
            log.info("Koneksi %s (%s:%s)", connection.name, connection.host, connection.port)
            errors.extend(
                backup_connection(
                    connection,
                    date_folder,
                    backup_dir,
                    drive,
                    table_logs,
                    state,
                    incremental,
                )
            )
        save_state(state_path, state)

        log_path = dated / "backup.log"
        write_backup_log(log_path, date_folder, now, table_logs)
        date_folder_id = drive.ensure_folder(drive.root_folder_id, date_folder)
        drive.upload_file(date_folder_id, log_path, mimetype="text/plain")
        log.info("Log run diunggah ke %s/backup.log (%s tabel)", date_folder, len(table_logs))
        for item in table_logs:
            level = log.info if item.status in {"OK", "SKIP"} else log.error
            level(
                "ringkasan %s %s/%s.%s %s %.1fs",
                item.status,
                item.connection,
                item.database,
                item.table,
                format_size(item.size_bytes),
                item.duration_s,
            )

        drive.apply_retention(retention_days, now)
    except Exception as exc:  # noqa: BLE001
        log.error("Backup dihentikan: %s", exc)
        return 1
    finally:
        if dated.exists():
            shutil.rmtree(dated, ignore_errors=True)

    if errors:
        log.error("Backup selesai dengan %s error", len(errors))
        for item in errors:
            log.error("  - %s", item)
        return 1

    dumped = sum(1 for item in table_logs if item.status == "OK")
    skipped = sum(1 for item in table_logs if item.status == "SKIP")
    log.info("Backup selesai: dump=%s skip=%s", dumped, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
