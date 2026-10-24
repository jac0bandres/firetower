"""FastAPI coordinator board: serves the static UI and a polling JSON API
over the SQLite ticket store. The board never reaches the network itself —
it only reads db.py and reports router.is_online() for the banner.

Set FIRETOWER_MOCK=1 to serve fixtures/mock_tickets.json from memory instead
of the real database, so the board/frontend can be built against the API
shape before db.py and triage.py are wired up.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

import db
import router
from logging_config import setup_logging

MOCK_MODE = os.environ.get("FIRETOWER_MOCK", "0") == "1"
MOCK_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mock_tickets.json"

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

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


@app.get("/api/tickets")
def tickets(status: str | None = None):
    if MOCK_MODE:
        if status:
            return [t for t in _mock_tickets if t["status"] == status]
        return _mock_tickets
    return db.list_tickets(status)


@app.post("/api/tickets/{ticket_code}/status")
def update_ticket_status(ticket_code: str, status: str):
    if MOCK_MODE:
        for t in _mock_tickets:
            if t["ticket_code"] == ticket_code:
                t["status"] = status
                return t
        return {"error": "not found"}
    db.set_ticket_status(ticket_code, status)
    return {"ticket_code": ticket_code, "status": status}


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
