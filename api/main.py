"""FastAPI: the record store's HTTP face, and the live panel the demo is recorded from.

DELIBERATE DEVIATION FROM PLAN.md: the UI is one self-contained HTML page (web/index.html),
vanilla JS, served from here. Not Next.js. A build toolchain hours before a deadline is a
real risk that earns zero points, and everything the panel has to do fits in one page.

Two ingest paths, ONE code path after that. The agent appends to events/{call_id}.jsonl;
the websocket tails that file, folds each line into SQLite, and pushes it to the browser.
Demo mode pushes the scripted fixture through the SAME fold, so the consent record a
rehearsal produces is built and hashed by exactly the code that serves a live call.

DEGRADING, not crashing: if a live call's JSONL never appears — the agent isn't up, LiveKit
isn't up — the socket waits FALLBACK_AFTER_S and then replays the fixture instead, after
first rewriting the call's mode to 'demo' so the record cannot be mistaken for a
measurement. There is always something to record.

    uv run uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api import demo, events, provider, records, store

WEB = Path(__file__).resolve().parent.parent / "web"

POLL_S = 0.15  # tail interval; also the UI's worst-case lag
FALLBACK_AFTER_S = 6.0  # silence on a 'live' call before the fixture takes over

app = FastAPI(title="SAMJHA", version="0.1.0")

# One connection, one write lock. sqlite3 is opened check_same_thread=False because
# FastAPI runs sync handlers on a threadpool; store.py's docstring promises the lock lives
# here, so here it is.
_conn = None
_conn_lock = threading.Lock()
_write_lock = threading.RLock()

# Byte offset per tailed file. Complete lines only — a half-written line is left for the
# next poll rather than parsed and dropped.
_offsets: dict[str, int] = {}


def conn():
    global _conn
    with _conn_lock:
        if _conn is None:
            _conn = store.connect()
        return _conn


def ingest(call_id: str, ev: dict) -> None:
    with _write_lock:
        store.apply_event(conn(), call_id, ev)


def drain_jsonl(call_id: str) -> int:
    """Fold any new complete lines of events/{call_id}.jsonl into the store.

    Called from the socket's loop AND from sync handlers on the threadpool, so the
    read-offset-then-advance is done under the write lock: interleaving it would replay a
    chunk of the file and duplicate transition rows in the record.
    """
    p = events.path_for(call_id)
    if not p.exists():
        return 0

    with _write_lock:
        off = _offsets.get(call_id, 0)
        with p.open("rb") as f:
            f.seek(off)
            data = f.read()
        if not data:
            return 0
        # A half-written line is left for the next poll rather than parsed and dropped.
        *lines, tail = data.split(b"\n")
        _offsets[call_id] = off + len(data) - len(tail)

        n = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except ValueError:
                continue  # a malformed line degrades the panel, never stops the demo
            for ev in events.expand(raw):
                store.apply_event(conn(), call_id, ev)
                n += 1
        return n


# ------------------------------------------------------------------------- HTTP


class NewCall(BaseModel):
    call_id: str | None = None
    mode: str = "live"
    label: str = ""
    kfs_ref: str = ""


@app.post("/calls")
def post_call(body: NewCall) -> dict:
    call_id = body.call_id or f"{body.mode}-{uuid.uuid4().hex[:8]}"
    with _write_lock:
        call = store.create_call(conn(), call_id, mode=body.mode, label=body.label,
                                 kfs_ref=body.kfs_ref, provider=provider.active())
    return call


@app.post("/demo")
def post_demo() -> dict:
    """A fresh call id per rehearsal, so a replay never lands on a dirty timeline.

    The websocket does the replaying: an asyncio task spawned from a sync handler dies
    with the request's event loop, and the socket's loop is alive for exactly as long as
    the panel is watching.
    """
    call_id = f"demo-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}"
    with _write_lock:
        store.create_call(conn(), call_id, mode="demo", label=demo.LABEL,
                          kfs_ref=demo.KFS_REF, provider=provider.active())
    return {"call_id": call_id, "mode": "demo", "duration_s": round(demo.duration_s(), 1)}


@app.get("/calls")
def get_calls() -> list[dict]:
    return store.list_calls(conn())


@app.get("/calls/{call_id}")
def get_call(call_id: str) -> dict:
    drain_jsonl(call_id)
    call = store.get_call(conn(), call_id)
    if call is None:
        raise HTTPException(404, f"no call {call_id!r}")
    return call | {
        "clauses": store.clauses(conn(), call_id),
        "consents": store.consents(conn(), call_id),
        "teachbacks": store.teachbacks(conn(), call_id),
        "active_provider": provider.active(),
    }


@app.get("/calls/{call_id}/record")
def get_record(call_id: str) -> dict:
    drain_jsonl(call_id)
    try:
        return records.sealed_record(conn(), call_id)
    except KeyError:
        raise HTTPException(404, f"no call {call_id!r}") from None


@app.get("/provider")
def get_provider() -> dict:
    return provider.active()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": str(store.db_path()), "events": str(events.events_dir())}


# ------------------------------------------------------------------------- pages
#
# Both routes serve the same file. The page reads location.pathname and shows either the
# live panel or the record viewer — one artifact to keep consistent instead of two.


def _page() -> FileResponse:
    return FileResponse(WEB / "index.html", media_type="text/html; charset=utf-8")


@app.get("/")
def index() -> FileResponse:
    return _page()


@app.get("/record/{call_id}")
def record_page(call_id: str) -> FileResponse:
    return _page()


# --------------------------------------------------------------------- websocket


async def replay_demo(call_id: str) -> None:
    """Push the scripted fixture through the same ingest path a live call uses."""
    with _write_lock:
        store.set_mode(conn(), call_id, "demo")
    ingest(call_id, events.normalize({"type": "provider", **provider.active()}))
    for delay, ev in demo.script():
        await asyncio.sleep(delay)
        ingest(call_id, events.normalize(ev))


@app.websocket("/calls/{call_id}/live")
async def live(ws: WebSocket, call_id: str) -> None:
    await ws.accept()
    call = store.get_call(conn(), call_id)
    if call is None:
        with _write_lock:
            call = store.create_call(conn(), call_id, provider=provider.active())

    await ws.send_json({
        "type": "hello", "call_id": call_id, "call": call,
        "provider": provider.active(), "states": list(events.CLAUSE_STATES),
        "poll_s": POLL_S,
    })

    seq = 0
    idle = 0.0
    task: asyncio.Task | None = None
    if call["mode"] == "demo" and store.event_count(conn(), call_id) == 0:
        task = asyncio.create_task(replay_demo(call_id))

    try:
        while True:
            drain_jsonl(call_id)
            batch = store.events_since(conn(), call_id, seq)
            if batch:
                idle = 0.0
                seq = batch[-1]["_seq"]
                for ev in batch:
                    await ws.send_json(ev)
            else:
                idle += POLL_S
                if task is None and idle >= FALLBACK_AFTER_S:
                    ingest(call_id, events.normalize({
                        "type": "note",
                        "text": "No agent events on this call. Falling back to the "
                                "built-in DEMO FIXTURE — this run is recorded as "
                                "mode=demo and is a rehearsal, not a measurement.",
                    }))
                    task = asyncio.create_task(replay_demo(call_id))
            await asyncio.sleep(POLL_S)
    except WebSocketDisconnect:
        pass
    finally:
        if task is not None:
            task.cancel()


def _demo() -> None:
    """Self-check with no server: the app boots, serves the page, and hashes a record."""
    import os
    import tempfile

    from fastapi.testclient import TestClient

    with tempfile.TemporaryDirectory() as td:
        os.environ["SAMJHA_DB"] = str(Path(td) / "t.db")
        os.environ["SAMJHA_EVENTS_DIR"] = str(Path(td) / "events")
        global _conn
        _conn = None

        with TestClient(app) as c:
            assert c.get("/").status_code == 200
            cid = c.post("/calls", json={"label": "self-check"}).json()["id"]

            events.emit(cid, "clause_registered", clause_id="apr", ordinal=0,
                        title_hi="वार्षिक प्रतिशत दर", total_duration_s=9.5)
            events.emit(cid, "consent", decision="REFUSED", reason="self-check",
                        flagged_for_callback=True)

            rec = c.get(f"/calls/{cid}/record").json()
            assert rec["record"]["refusals"], "a refusal must survive to the record"
            assert rec["hash"] == records.record_hash(rec["record"])
            print(f"api OK — page served, record sha256 {rec['hash'][:16]}…, "
                  f"consent {rec['record']['consent']['decision']}")


if __name__ == "__main__":
    _demo()
