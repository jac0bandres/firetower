"""FastAPI coordinator board: serves the static UI and a polling JSON API
over the SQLite ticket store. The board never reaches the network itself —
it only reads db.py and reports router.is_online() for the banner."""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
import router

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
def startup():
    db.init_db()


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/status")
def status():
    return {"online": router.is_online()}


@app.get("/api/tickets")
def tickets(status: str | None = None):
    raise NotImplementedError


@app.post("/api/tickets/{ticket_code}/status")
def update_ticket_status(ticket_code: str, status: str):
    raise NotImplementedError


@app.get("/api/priority_nodes")
def priority_nodes():
    raise NotImplementedError


@app.post("/api/priority_nodes/{node_id}")
def set_priority_node(node_id: str, role: str):
    raise NotImplementedError
