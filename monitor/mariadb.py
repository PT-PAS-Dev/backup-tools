from __future__ import annotations

from typing import Any

import pymysql

from monitor.config import SYSTEM_SCHEMAS, Node, env_key

SOURCE_OK_PREFIXES = ("SELECT", "SHOW")
CONNECT_TIMEOUT = 6
READ_TIMEOUT_LIGHT = 25
READ_TIMEOUT_HEAVY = 90


class MariaError(Exception):
    pass


class SafetyError(Exception):
    pass


def _assert_source_readonly(sql: str) -> None:
    token = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    if token not in SOURCE_OK_PREFIXES:
        raise SafetyError(f"Refusing non-read SQL on source: {token}")


def connect(
    node: Node,
    database: str | None = None,
    *,
    read_timeout: int | None = None,
) -> pymysql.connections.Connection:
    if not node.user:
        hints: list[str] = []
        if node.connection:
            hints.append(env_key("MYSQL_USER", node.connection))
            hints.append(env_key("MYSQL_PASSWORD", node.connection))
        hints.append(env_key("CLONE_MYSQL_USER", node.name))
        hints.append(env_key("CLONE_MYSQL_PASSWORD", node.name))
        env_hint = ", ".join(dict.fromkeys(hints))
        raise MariaError(
            f"{node.name}: MySQL user is not configured — set in .env (e.g. {env_hint}) "
            "then: docker compose up -d --force-recreate monitor"
        )
    rt = read_timeout if read_timeout is not None else READ_TIMEOUT_LIGHT
    try:
        conn = pymysql.connect(
            host=node.host,
            port=node.port,
            user=node.user,
            password=node.password,
            database=database,
            charset="utf8mb4",
            ssl_disabled=True,
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=rt,
            write_timeout=rt,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.Error as exc:
        raise MariaError(f"{node.name} ({node.host}): {exc}") from exc
    return conn


def query(node: Node, sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    if node.role == "source":
        _assert_source_readonly(sql)
    conn = connect(node)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() or []
        return list(rows)
    except pymysql.Error as exc:
        raise MariaError(f"{node.name}: {exc}") from exc
    finally:
        conn.close()


def query_one(node: Node, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    rows = query(node, sql, params)
    return rows[0] if rows else None


def execute_clone(node: Node, sql: str, params: tuple[Any, ...] | None = None) -> None:
    if node.role != "clone":
        raise SafetyError(f"Refusing write on non-clone {node.name} ({node.role})")
    conn = connect(node)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
    except pymysql.Error as exc:
        raise MariaError(f"{node.name}: {exc}") from exc
    finally:
        conn.close()


def _map_rows(rows: list[dict[str, Any]], key: str = "Variable_name", value: str = "Value") -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        if key in row:
            result[str(row[key])] = "" if row.get(value) is None else str(row[value])
        else:
            items = list(row.items())
            if len(items) >= 2:
                result[str(items[0][1])] = "" if items[1][1] is None else str(items[1][1])
    return result


def collect(node: Node, include_tables: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": node.name,
        "host": node.host,
        "port": node.port,
        "role": node.role,
        "os_type": node.os,
        "online": False,
        "error": "",
        "variables": {},
        "status": {},
        "master": {},
        "slave": None,
        "gtid": {},
        "binary_logs": [],
        "databases": [],
        "tables": [],
        "size_bytes": 0,
        "mariadb_uptime_seconds": None,
        "read_only": None,
        "version": "",
    }
    read_timeout = READ_TIMEOUT_HEAVY if include_tables else READ_TIMEOUT_LIGHT
    try:
        conn = connect(node, read_timeout=read_timeout)
    except MariaError as exc:
        payload["error"] = str(exc)
        return payload

    try:
        payload["online"] = True
        with conn.cursor() as cur:
            cur.execute(
                """
                SHOW GLOBAL VARIABLES WHERE Variable_name IN (
                    'server_id', 'log_bin', 'binlog_format', 'log_slave_updates',
                    'expire_logs_days', 'binlog_expire_logs_seconds',
                    'gtid_strict_mode', 'gtid_domain_id', 'read_only', 'version',
                    'datadir', 'hostname'
                )
                """
            )
            payload["variables"] = _map_rows(list(cur.fetchall() or []))
            payload["version"] = payload["variables"].get("version", "")
            payload["read_only"] = payload["variables"].get("read_only", "").lower() in {"on", "1", "true"}

            cur.execute(
                """
                SHOW GLOBAL STATUS WHERE Variable_name IN (
                    'Uptime', 'Threads_connected', 'Questions', 'Slow_queries'
                )
                """
            )
            payload["status"] = _map_rows(list(cur.fetchall() or []))
            uptime = payload["status"].get("Uptime")
            payload["mariadb_uptime_seconds"] = int(uptime) if uptime and uptime.isdigit() else None

            try:
                cur.execute("SELECT @@gtid_current_pos AS current_pos, @@gtid_binlog_pos AS binlog_pos, @@gtid_slave_pos AS slave_pos")
                gtid_row = cur.fetchone() or {}
                payload["gtid"] = {
                    "current_pos": gtid_row.get("current_pos") or "",
                    "binlog_pos": gtid_row.get("binlog_pos") or "",
                    "slave_pos": gtid_row.get("slave_pos") or "",
                }
            except pymysql.Error as exc:
                payload["gtid"] = {"error": str(exc)}

            log_bin_on = str(payload["variables"].get("log_bin") or "").lower() in {"on", "1", "true"}
            if node.role == "source" or log_bin_on:
                try:
                    cur.execute("SHOW MASTER STATUS")
                    master = cur.fetchone() or {}
                    payload["master"] = {str(k): ("" if v is None else v) for k, v in master.items()}
                except pymysql.Error as exc:
                    payload["master"] = {"error": str(exc)}
            else:
                payload["master"] = {}

            if log_bin_on:
                try:
                    cur.execute("SHOW BINARY LOGS")
                    logs = []
                    for row in cur.fetchall() or []:
                        logs.append(
                            {
                                "name": row.get("Log_name") or row.get("Name") or "",
                                "size": int(row.get("File_size") or 0),
                            }
                        )
                    payload["binary_logs"] = logs
                except pymysql.Error:
                    payload["binary_logs"] = []
            else:
                payload["binary_logs"] = []

            try:
                cur.execute("SHOW SLAVE STATUS")
                slave = cur.fetchone()
                if slave:
                    payload["slave"] = _normalize_slave(slave)
                else:
                    payload["slave"] = None
            except pymysql.Error as exc:
                payload["slave"] = {"error": str(exc)}

            cur.execute(
                """
                SELECT
                    table_schema AS db_name,
                    COUNT(*) AS table_count,
                    COALESCE(SUM(data_length + index_length), 0) AS size_bytes,
                    COALESCE(SUM(table_rows), 0) AS approx_rows
                FROM information_schema.tables
                WHERE table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
                GROUP BY table_schema
                ORDER BY size_bytes DESC
                """
            )
            databases = []
            total = 0
            for row in cur.fetchall() or []:
                size = int(row["size_bytes"] or 0)
                total += size
                databases.append(
                    {
                        "name": row["db_name"],
                        "table_count": int(row["table_count"] or 0),
                        "size_bytes": size,
                        "approx_rows": int(row["approx_rows"] or 0),
                    }
                )
            payload["databases"] = databases
            payload["size_bytes"] = total

            if include_tables:
                cur.execute(
                    """
                    SELECT
                        table_schema AS db_name,
                        table_name AS table_name,
                        engine AS engine,
                        table_type AS table_type,
                        COALESCE(data_length + index_length, 0) AS size_bytes,
                        COALESCE(table_rows, 0) AS approx_rows
                    FROM information_schema.tables
                    WHERE table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
                    ORDER BY table_schema, table_name
                    """
                )
                tables = []
                for row in cur.fetchall() or []:
                    tables.append(
                        {
                            "schema": row["db_name"],
                            "name": row["table_name"],
                            "engine": row["engine"] or "",
                            "table_type": row["table_type"] or "",
                            "size_bytes": int(row["size_bytes"] or 0),
                            "approx_rows": int(row["approx_rows"] or 0),
                        }
                    )
                payload["tables"] = tables
        return payload
    except pymysql.Error as exc:
        payload["error"] = f"{node.name}: {exc}"
        return payload
    finally:
        conn.close()


def _normalize_slave(row: dict[str, Any]) -> dict[str, Any]:
    def val(*keys: str) -> Any:
        for key in keys:
            if key in row and row[key] is not None:
                return row[key]
        return ""

    lag_raw = val("Seconds_Behind_Master")
    lag = None
    if lag_raw != "" and lag_raw is not None:
        try:
            lag = int(lag_raw)
        except (TypeError, ValueError):
            lag = None

    last_error = str(val("Last_Error") or val("Last_SQL_Error") or val("Last_IO_Error") or "")
    last_errno = val("Last_Errno", "Last_SQL_Errno", "Last_IO_Errno")
    return {
        "io_running": str(val("Slave_IO_Running") or ""),
        "sql_running": str(val("Slave_SQL_Running") or ""),
        "seconds_behind": lag,
        "last_error": last_error,
        "last_errno": last_errno,
        "last_io_error": str(val("Last_IO_Error") or ""),
        "last_sql_error": str(val("Last_SQL_Error") or ""),
        "last_error_time": str(val("Last_IO_Error_Timestamp") or val("Last_SQL_Error_Timestamp") or val("Last_Error_Timestamp") or ""),
        "master_host": str(val("Master_Host") or ""),
        "master_user": str(val("Master_User") or ""),
        "master_port": val("Master_Port"),
        "master_log_file": str(val("Master_Log_File") or ""),
        "read_master_log_pos": val("Read_Master_Log_Pos"),
        "relay_master_log_file": str(val("Relay_Master_Log_File") or ""),
        "exec_master_log_pos": val("Exec_Master_Log_Pos"),
        "using_gtid": str(val("Using_Gtid") or ""),
        "gtid_io_pos": str(val("Gtid_IO_Pos") or ""),
        "retrieved_gtid_set": str(val("Retrieved_Gtid_Set") or ""),
        "executed_gtid_set": str(val("Executed_Gtid_Set") or ""),
        "slave_sql_running_state": str(val("Slave_SQL_Running_State") or ""),
        "last_io_errno": val("Last_IO_Errno"),
        "last_sql_errno": val("Last_SQL_Errno"),
    }


def application_databases(payload: dict[str, Any]) -> list[str]:
    names = [item["name"] for item in payload.get("databases") or [] if item.get("name") not in SYSTEM_SCHEMAS]
    return names
