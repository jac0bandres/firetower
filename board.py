"""FastAPI coordinator board: serves the static UI and a polling JSON API
over the SQLite ticket store. The board never reaches the network itself —
it only reads db.py and reports router.is_online() for the banner.

Set FIRETOWER_MOCK=1 to serve fixtures/mock_tickets.json from memory for a
static demo. The default is FIRETOWER_MOCK=0, which serves the SQLite database.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import db
import dispatch
import facilities
import router
import routing
from logging_config import setup_logging

logger = logging.getLogger(__name__)

MOCK_MODE = os.environ.get("FIRETOWER_MOCK", "0") == "1"
MOCK_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mock_tickets.json"

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def revalidate_ui_assets(request: Request, call_next):
    response = await call_next(request)
    # Coordinator actions are logged; the 2-second polling GETs are not.
    if request.method != "GET":
        logger.info("board %s %s?%s -> %s", request.method, request.url.path,
                    request.url.query, response.status_code)
    elif response.status_code >= 400:
        logger.warning("board GET %s -> %s", request.url.path, response.status_code)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    return response


_mock_tickets: list[dict] = []
_mock_priority_nodes: dict[str, str] = {
    "!a1b2c3d4": "registered medical need",
    "!a1b2c3d7": "shelter lead",
}


@app.on_event("startup")
def startup():
    global _mock_tickets
    setup_logging()
    if MOCK_MODE:
        _mock_tickets = json.loads(MOCK_FIXTURE_PATH.read_text())
    else:
        db.init_db()


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/status")
def status():
    return {"online": True if MOCK_MODE else router.is_online(), "mock": MOCK_MODE}


def present_ticket(ticket: dict) -> dict:
    presented = dict(ticket)
    raw = presented.get("dispatched_teams")
    if isinstance(raw, str):
        presented["dispatched_teams"] = [part for part in raw.split(",") if part]
    elif not raw:
        presented["dispatched_teams"] = []
    presented["suggested_teams"] = dispatch.suggest_teams(ticket)
    return presented


def _find_ticket(ticket_code: str) -> dict | None:
    rows = _mock_tickets if MOCK_MODE else db.list_tickets()
    return next((ticket for ticket in rows if ticket["ticket_code"] == ticket_code), None)


@app.get("/api/tickets")
def tickets(status: str | None = None):
    if MOCK_MODE:
        rows = _mock_tickets
        if status:
            rows = [t for t in rows if t["status"] == status]
    else:
        rows = db.list_tickets(status)
    return [present_ticket(ticket) for ticket in rows]


@app.post("/api/tickets/{ticket_code}/status")
def update_ticket_status(ticket_code: str, status: str):
    if MOCK_MODE:
        for t in _mock_tickets:
            if t["ticket_code"] == ticket_code:
                t["status"] = status
                if status == "open":
                    t["dispatched_teams"] = None
                    t["dispatched_at"] = None
                return t
        return {"error": "not found"}
    db.set_ticket_status(ticket_code, status)
    return {"ticket_code": ticket_code, "status": status}


@app.post("/api/tickets/{ticket_code}/dispatch")
def dispatch_ticket(ticket_code: str):
    """Record the teams chosen from the report. Does not contact anyone."""
    ticket = _find_ticket(ticket_code)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    teams = dispatch.suggest_teams(ticket)
    if not teams:
        raise HTTPException(status_code=400, detail="No response team matched this report")
    if MOCK_MODE:
        now = datetime.now(timezone.utc).isoformat()
        ticket["dispatched_teams"] = ",".join(teams)
        ticket["dispatched_at"] = now
        ticket["status"] = "assigned"
        ticket["assigned_at"] = now
    else:
        try:
            db.set_ticket_dispatch(ticket_code, teams)
        except LookupError:
            raise HTTPException(status_code=404, detail="Ticket not found") from None
        # The gateway holds the radio; it sends queued DMs within a few seconds.
        db.queue_outbox(ticket["node_id"], ticket_code, f"dispatch notice {ticket_code}",
                        dispatch.sender_notice(teams))
        ticket = _find_ticket(ticket_code)
    logger.info("dispatch recorded ticket=%s teams=%s; notice queued for %s",
                ticket_code, ",".join(teams), ticket["node_id"])
    return present_ticket(ticket)


@app.get("/api/route")
def street_route(from_lon: float, from_lat: float, to_lon: float, to_lat: float):
    """Driving route for the simulated dispatch animation (OSRM, or a straight line)."""
    return routing.drive((from_lon, from_lat), (to_lon, to_lat))


@app.get("/api/facilities")
def list_facilities():
    """Fire stations, hospitals and police around the mesh (OpenStreetMap)."""
    return facilities.load()


@app.get("/api/priority_nodes")
def priority_nodes():
    if MOCK_MODE:
        return _mock_priority_nodes
    return db.list_priority_nodes()


@app.post("/api/priority_nodes/{node_id}")
def set_priority_node(node_id: str, role: str):
    if MOCK_MODE:
        _mock_priority_nodes[node_id] = role
        return {"node_id": node_id, "role": role}
    db.set_priority_node(node_id, role)
    return {"node_id": node_id, "role": role}


@app.delete("/api/priority_nodes/{node_id}")
def remove_priority_node(node_id: str):
    if MOCK_MODE:
        _mock_priority_nodes.pop(node_id, None)
        return {"node_id": node_id, "removed": True}
    db.remove_priority_node(node_id)
    return {"node_id": node_id, "removed": True}
