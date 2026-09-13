from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from monitor import clone, store
from monitor.collector import cached_overview, compact_node, poll_once
from monitor.config import load_topology
from monitor.status import binlog_retention_risk, enrich, format_size, verify_pair

log = logging.getLogger("clone-monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JAKARTA = ZoneInfo("Asia/Jakarta")
ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["filesize"] = format_size

scheduler = BackgroundScheduler(timezone=JAKARTA)


def _poll() -> None:
    try:
        poll_once()
    except Exception:
        log.exception("poll failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init()
    topology = load_topology()
    interval = max(15, topology.thresholds.poll_interval_seconds)
    scheduler.add_job(_poll, "interval", seconds=interval, id="poll", replace_existing=True, next_run_time=datetime.now(JAKARTA))
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Dashboard Clone DB", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def _page(
    request: Request,
    name: str,
    extra: dict[str, Any] | None = None,
    *,
    with_overview: bool = True,
) -> HTMLResponse:
    topology = load_topology()
    overview = cached_overview(topology) if with_overview else {"nodes": [], "missing": topology.missing}
    context = {
        "request": request,
        "nav": name,
        "topology": topology,
        "overview": overview,
        "title": "Dashboard Clone DB",
    }
    if extra:
        context.update(extra)
    return templates.TemplateResponse(request, f"{name}.html", context)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return _page(request, "dashboard")


def _clone_database_catalog(target_name: str) -> dict[str, Any]:
    try:
        return clone.list_databases(target_name)
    except Exception as exc:  # noqa: BLE001
        return {
            "target": target_name,
            "source": "",
            "source_host": "",
            "databases": [],
            "total_size_human": "",
            "error": str(exc),
        }


@app.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request) -> HTMLResponse:
    progress = clone.progress_payload(include_sync=False)
    default_target = progress["clones"][0]["name"] if progress.get("clones") else ""
    catalog = _clone_database_catalog(default_target) if default_target else {"databases": [], "error": ""}
    return _page(
        request,
        "progress",
        {
            "progress": progress,
            "catalog": catalog,
            "catalog_target": default_target,
        },
        with_overview=False,
    )


@app.get("/replication", response_class=HTMLResponse)
def replication_page(request: Request) -> HTMLResponse:
    return _page(request, "replication", {"links": _replication_links()})


@app.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request) -> HTMLResponse:
    return _page(request, "verify", {"pairs": _verification_pairs()})


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    return _page(request, "history")


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request) -> HTMLResponse:
    return _page(request, "alerts", {"alerts": store.list_alerts(300)})


@app.get("/api/overview")
def api_overview() -> dict[str, Any]:
    return cached_overview()


@app.get("/api/replication")
def api_replication() -> dict[str, Any]:
    return {"links": _replication_links()}


@app.get("/api/verify")
def api_verify() -> dict[str, Any]:
    return {"pairs": _verification_pairs()}


@app.get("/api/progress")
def api_progress() -> dict[str, Any]:
    return clone.progress_payload()


@app.get("/api/alerts")
def api_alerts() -> dict[str, Any]:
    return {"alerts": store.list_alerts(300)}


@app.get("/api/history")
def api_history(range: str = "24h") -> dict[str, Any]:
    mapping = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
    }
    delta = mapping.get(range, mapping["24h"])
    since = (datetime.now(JAKARTA) - delta).isoformat(timespec="seconds")
    topology = load_topology()
    names = [node.name for node in topology.nodes()]
    metrics = ["lag_seconds", "size_bytes", "cpu_percent", "ram_percent", "disk_percent", "online"]
    series = {metric: store.metrics_series(metric, since, names) for metric in metrics}
    return {"range": range, "since": since, "series": series, "servers": names}


@app.get("/api/clone/databases")
def api_clone_databases(target: str) -> JSONResponse:
    try:
        return JSONResponse(clone.list_databases(target))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/clone/precheck")
async def api_precheck(request: Request) -> JSONResponse:
    body = await request.json()
    target = str(body.get("target") or "")
    raw_dbs = body.get("databases")
    selected = None if raw_dbs is None else [str(item) for item in raw_dbs]
    try:
        result = clone.precheck(target, selected_databases=selected)
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/clone/start")
async def api_start(request: Request) -> JSONResponse:
    body = await request.json()
    ip = request.client.host if request.client else ""
    raw_dbs = body.get("databases")
    selected = None if raw_dbs is None else [str(item) for item in raw_dbs]
    try:
        result = clone.start_job(
            target_name=str(body.get("target") or ""),
            typed_confirm=str(body.get("confirm") or ""),
            replace=bool(body.get("replace")),
            ip=ip,
            selected_databases=selected,
        )
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001
        store.insert_audit("admin", "CLONE_START", str(body.get("target") or ""), "rejected", str(exc)[:500], ip)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


def _replication_links() -> list[dict[str, Any]]:
    topology = load_topology()
    overview = cached_overview(topology)
    by_name = {node["name"]: node for node in overview["nodes"]}
    links = []
    for clone_node in topology.clones:
        upstream = topology.upstream(clone_node)
        if not upstream:
            continue
        src = by_name.get(upstream.name) or {}
        dst = by_name.get(clone_node.name) or {}
        slave = dst.get("slave") if isinstance(dst.get("slave"), dict) else None
        risk = binlog_retention_risk(src, dst, topology.thresholds.binlog_risk_ratio) if src and dst else None
        links.append(
            {
                "from": upstream.name,
                "from_host": upstream.host,
                "to": clone_node.name,
                "to_host": clone_node.host,
                "source": compact_node(src),
                "replica": compact_node(dst),
                "slave": slave,
                "risk": risk,
            }
        )
    return links


def _verification_pairs() -> list[dict[str, Any]]:
    topology = load_topology()
    raw = store.latest_inventories() or store.latest_snapshots()
    cloning = {job["target"] for job in store.active_jobs()}
    pairs = []
    for clone_node in topology.clones:
        upstream = topology.upstream(clone_node)
        if not upstream:
            continue
        src = enrich(raw.get(upstream.name) or {"name": upstream.name, "online": False, "databases": [], "tables": [], "size_bytes": 0}, topology)
        dst = enrich(raw.get(clone_node.name) or {"name": clone_node.name, "online": False, "databases": [], "tables": [], "size_bytes": 0, "role": "clone"}, topology, cloning=clone_node.name in cloning)
        pairs.append(
            {
                "from": upstream.name,
                "to": clone_node.name,
                "from_host": upstream.host,
                "to_host": clone_node.host,
                "result": verify_pair(src, dst),
            }
        )
    return pairs
