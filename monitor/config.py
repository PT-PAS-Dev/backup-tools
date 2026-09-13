from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from paths import PROJECT_ROOT, load_project_env

load_project_env()

SYSTEM_SCHEMAS = frozenset({"information_schema", "performance_schema", "mysql", "sys"})


def env_key(prefix: str, name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
    return f"{prefix}_{normalized}"


def _opt_env(*keys: str) -> str:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return ""


def resolve_config_path() -> Path:
    raw = os.environ.get("CONFIG_PATH", "")
    candidates: list[Path] = []
    if raw:
        path = Path(raw)
        candidates.append(path if path.is_absolute() else PROJECT_ROOT / path)
    candidates.extend(
        [
            PROJECT_ROOT / "config.yaml",
            PROJECT_ROOT / "config" / "config.yaml",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


@dataclass
class MonitorThresholds:
    poll_interval_seconds: int = 30
    lag_warning_seconds: int = 60
    disk_low_percent: int = 15
    cpu_high_percent: int = 90
    ram_high_percent: int = 90
    alert_cooldown_minutes: int = 30
    growth_percent_24h: int = 20
    binlog_risk_ratio: float = 0.5


@dataclass
class Node:
    name: str
    host: str
    port: int
    role: str
    os: str
    user: str = ""
    password: str = ""
    replicate_from: str = ""
    ssh_user: str = ""
    ssh_port: int = 22
    ssh_password: str = ""
    ssh_key: str = ""
    connection: str = ""
    label: str = ""
    local_mysql_host: str = ""
    docker_container: str = ""
    docker_sudo: bool = False
    ssh_sudo_password: str = ""

    @property
    def display_host(self) -> str:
        parts = self.host.split(".")
        if len(parts) == 4:
            return f".{parts[-1]}"
        return self.host

    @property
    def title(self) -> str:
        if self.label:
            return self.label
        if self.role == "source":
            return "PRODUCTION MASTER"
        return "DATABASE CLONE"


@dataclass
class Topology:
    source: Node | None = None
    clones: list[Node] = field(default_factory=list)
    thresholds: MonitorThresholds = field(default_factory=MonitorThresholds)
    config_path: Path = field(default_factory=resolve_config_path)
    missing: str = ""

    def nodes(self) -> list[Node]:
        items: list[Node] = []
        if self.source:
            items.append(self.source)
        items.extend(self.clones)
        return items

    def by_name(self, name: str) -> Node | None:
        for node in self.nodes():
            if node.name == name:
                return node
        return None

    def upstream(self, node: Node) -> Node | None:
        if not node.replicate_from:
            return None
        return self.by_name(node.replicate_from)

    def is_source_host(self, host: str) -> bool:
        if not self.source:
            return False
        return host.strip() == self.source.host.strip()


def _as_node(raw: dict[str, Any], role: str) -> Node:
    name = str(raw.get("name") or raw.get("host") or role)
    host = str(raw.get("host") or "")
    if not host:
        raise ValueError(f"topology node {name!r} needs host")
    node = Node(
        name=name,
        host=host,
        port=int(raw.get("port") or 3306),
        role=str(raw.get("role") or role),
        os=str(raw.get("os") or "linux"),
        user=str(raw.get("user") or ""),
        password=str(raw.get("password") or ""),
        replicate_from=str(raw.get("replicate_from") or ""),
        ssh_user=str(raw.get("ssh_user") or ""),
        ssh_port=int(raw.get("ssh_port") or 22),
        connection=str(raw.get("connection") or ""),
        label=str(raw.get("label") or ""),
        local_mysql_host=str(raw.get("local_mysql_host") or ""),
        docker_container=str(raw.get("docker_container") or ""),
        docker_sudo=bool(raw.get("docker_sudo")),
    )
    node.user = (
        _opt_env(env_key("CLONE_MYSQL_USER", name), "CLONE_MYSQL_USER")
        or _opt_env(env_key("MONITOR_MYSQL_USER", name), "MONITOR_MYSQL_USER")
        or node.user
    )
    node.password = (
        _opt_env(env_key("CLONE_MYSQL_PASSWORD", name), "CLONE_MYSQL_PASSWORD")
        or _opt_env(env_key("MONITOR_MYSQL_PASSWORD", name), "MONITOR_MYSQL_PASSWORD")
        or node.password
    )
    node.ssh_user = (
        _opt_env(env_key("CLONE_SSH_USER", name), "CLONE_SSH_USER")
        or _opt_env(env_key("MONITOR_SSH_USER", name), "MONITOR_SSH_USER")
        or node.ssh_user
    )
    node.ssh_password = _opt_env(env_key("CLONE_SSH_PASSWORD", name), "CLONE_SSH_PASSWORD") or _opt_env(
        env_key("MONITOR_SSH_PASSWORD", name), "MONITOR_SSH_PASSWORD"
    )
    node.ssh_key = _opt_env(env_key("CLONE_SSH_KEY", name), "CLONE_SSH_KEY") or _opt_env(
        env_key("MONITOR_SSH_KEY", name), "MONITOR_SSH_KEY"
    )
    node.docker_container = (
        _opt_env(env_key("CLONE_DOCKER_CONTAINER", name), "CLONE_DOCKER_CONTAINER")
        or node.docker_container
    )
    sudo_env = _opt_env(env_key("CLONE_DOCKER_SUDO", name), "CLONE_DOCKER_SUDO")
    if sudo_env:
        node.docker_sudo = sudo_env.lower() in {"1", "true", "yes", "on"}
    node.ssh_sudo_password = (
        _opt_env(env_key("CLONE_SSH_SUDO_PASSWORD", name), "CLONE_SSH_SUDO_PASSWORD")
        or node.ssh_sudo_password
    )
    return node


def _apply_backup_connection(node: Node, connections: list[dict[str, Any]]) -> None:
    match: dict[str, Any] | None = None
    if node.connection:
        for item in connections:
            if str(item.get("name") or "") == node.connection:
                match = item
                break
    if match is None:
        for item in connections:
            if str(item.get("host") or "") == node.host and int(item.get("port") or 3306) == node.port:
                match = item
                break
    if not match:
        return
    if not node.user:
        node.user = str(match.get("user") or "")
    if not node.password:
        conn_name = str(match.get("name") or "")
        env_password = os.environ.get(env_key("MYSQL_PASSWORD", conn_name))
        node.password = env_password if env_password is not None else str(match.get("password") or "")


def load_topology(config_path: Path | None = None) -> Topology:
    path = config_path or resolve_config_path()
    topology = Topology(config_path=path)
    if not path.is_file():
        topology.missing = f"Config file not found: {path}"
        return topology

    data = yaml.safe_load(path.read_text()) or {}
    raw_topology = data.get("topology") or {}
    connections = data.get("connections") or []
    raw_thresholds = (
        data.get("clone_panel")
        or data.get("monitor")
        or raw_topology.get("clone_panel")
        or raw_topology.get("monitor")
        or {}
    )
    topology.thresholds = MonitorThresholds(
        poll_interval_seconds=int(raw_thresholds.get("poll_interval_seconds") or 30),
        lag_warning_seconds=int(raw_thresholds.get("lag_warning_seconds") or 60),
        disk_low_percent=int(raw_thresholds.get("disk_low_percent") or 15),
        cpu_high_percent=int(raw_thresholds.get("cpu_high_percent") or 90),
        ram_high_percent=int(raw_thresholds.get("ram_high_percent") or 90),
        alert_cooldown_minutes=int(raw_thresholds.get("alert_cooldown_minutes") or 30),
        growth_percent_24h=int(raw_thresholds.get("growth_percent_24h") or 20),
        binlog_risk_ratio=float(raw_thresholds.get("binlog_risk_ratio") or 0.5),
    )

    raw_source = raw_topology.get("source")
    if not raw_source:
        topology.missing = "config.yaml has no topology.source — add the clone topology block."
        return topology

    source = _as_node(raw_source, "source")
    source.role = "source"
    _apply_backup_connection(source, connections)
    topology.source = source

    for raw in raw_topology.get("clones") or []:
        clone = _as_node(raw, "clone")
        clone.role = "clone"
        _apply_backup_connection(clone, connections)
        topology.clones.append(clone)

    if not topology.clones:
        topology.missing = "config.yaml topology.clones is empty."
    return topology


def monitor_data_dir() -> Path:
    raw = os.environ.get("MONITOR_DATA_DIR", "monitor-data")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def sqlite_path() -> Path:
    raw = os.environ.get("MONITOR_DB_PATH")
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return monitor_data_dir() / "monitor.sqlite"


def replication_credentials() -> tuple[str, str]:
    return (
        os.environ.get("CLONE_REPL_USER") or os.environ.get("MONITOR_REPL_USER", ""),
        os.environ.get("CLONE_REPL_PASSWORD") or os.environ.get("MONITOR_REPL_PASSWORD", ""),
    )
