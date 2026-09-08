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
import os
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api import demo, events, intake, provider, records, store
from kfs.extract import UnsupportedDocument

# `uv run uvicorn api.main:app` does not load .env by itself — only agent/main.py did,
# which meant api/provider.py silently reported the fallback STT and a LiveKit token
# route would have found no credentials at all.
load_dotenv()

WEB = Path(__file__).resolve().parent.parent / "web"

POLL_S = 0.15  # tail interval; also the UI's worst-case lag
FALLBACK_AFTER_S = 6.0  # silence on a 'live' call before the fixture takes over

# Must match `@server.rtc_session(agent_name=...)` in agent/main.py, or a dispatch is
# created for a worker that never registered and the borrower waits forever.
AGENT_NAME = "samjha-consent"

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
    doc_id: str = ""


@app.post("/calls")
def post_call(body: NewCall) -> dict:
    """Create a call, optionally bound to an uploaded document.

    A `doc_id` must name a CONFIRMED document. Binding a call to a draft would let a
    borrower be dialled about a loan whose numbers nobody has finished reading.
    """
    label, kfs_ref = body.label, body.kfs_ref
    if body.doc_id:
        doc = store.get_document(conn(), body.doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {body.doc_id!r}")
        if not doc["kfs"]:
            raise HTTPException(
                409,
                f"document {body.doc_id!r} is still a draft: "
                f"{intake.missing_of(doc['report'], doc['corrections'])} not read yet",
            )
        kfs_ref = kfs_ref or _kfs_ref(doc)
        label = label or _call_label(doc)

    call_id = body.call_id or f"{body.mode}-{uuid.uuid4().hex[:8]}"
    with _write_lock:
        call = store.create_call(conn(), call_id, mode=body.mode, label=label,
                                 kfs_ref=kfs_ref, provider=provider.active(),
                                 doc_id=body.doc_id)
    return call


def _kfs_ref(doc: dict) -> str:
    """A human-readable citation of the source document, hash included."""
    kind = "synthetic" if doc["synthetic"] else "REAL BORROWER DATA"
    name = doc["filename"] or doc["doc_id"]
    return f"{name} ({kind}; sha256 {doc['sha256'][:12]}…; read by {doc['method']})"


def _call_label(doc: dict) -> str:
    kfs = intake.kfs_of(doc)
    return f"{kfs.loan_type} — {kfs.proposal_no}" if kfs else doc["doc_id"]


# ------------------------------------------------------------------------- intake


@app.post("/kfs")
async def post_kfs(request: Request, filename: str = "", synthetic: bool = True,
                   role: str = "lender_system") -> dict:
    """Upload a KFS as PDF or Word. The file is the RAW request body.

    Not multipart: `python-multipart` is not installed, so FastAPI's `UploadFile`/`File`
    would fail at request time. A raw body is also less code on both sides —
    `fetch(url, {method: "POST", body: file})` in the browser.

    The cap is enforced WHILE streaming, so an oversized upload is refused without ever
    being held in memory.
    """
    data = await _read_capped(request)
    try:
        with _write_lock:
            doc = intake.draft(conn(), data, filename=filename, synthetic=synthetic,
                               uploaded_by_role=role)
    except UnsupportedDocument as e:
        raise HTTPException(415, str(e)) from None
    except intake.RealDataRefused as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return _doc_view(doc)


async def _read_capped(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > intake.MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"upload exceeds {intake.MAX_UPLOAD_BYTES} bytes; a KFS is two tables"
            )
        chunks.append(chunk)
    if not chunks:
        raise HTTPException(400, "empty request body — send the file as the raw body")
    return b"".join(chunks)


class Corrections(BaseModel):
    values: dict[str, str]
    by: str = ""


@app.patch("/kfs/{doc_id}")
def patch_kfs(doc_id: str, body: Corrections) -> dict:
    """Supply values the document did not yield. Recorded as human-supplied, with `by`."""
    try:
        with _write_lock:
            doc = intake.apply_corrections(conn(), doc_id, body.values, by=body.by)
    except KeyError:
        raise HTTPException(404, f"no document {doc_id!r}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return _doc_view(doc)


@app.get("/kfs")
def get_kfs_list() -> list[dict]:
    return store.list_documents(conn())


@app.get("/kfs/{doc_id}")
def get_kfs(doc_id: str) -> dict:
    doc = store.get_document(conn(), doc_id)
    if doc is None:
        raise HTTPException(404, f"no document {doc_id!r}")
    return _doc_view(doc)


@app.get("/calls/{call_id}/kfs")
def get_call_kfs(call_id: str) -> dict:
    """The KFS for a call, by call id. THIS is how the agent learns which loan to read.

    Replaces the process-global `SAMJHA_KFS` env var: the agent already computes
    `call_id = ctx.room.name`, so it fetches per job and two concurrent borrowers cannot
    be served the same loan. `sha256` is returned so the agent can re-verify the document
    its facts came from.
    """
    call = store.get_call(conn(), call_id)
    if call is None:
        raise HTTPException(404, f"no call {call_id!r}")
    doc = store.get_document(conn(), call["doc_id"]) if call["doc_id"] else None
    if doc is None or not doc["kfs"]:
        raise HTTPException(404, f"call {call_id!r} has no confirmed document")
    return {
        "call_id": call_id,
        "doc_id": doc["doc_id"],
        "sha256": doc["sha256"],
        "kfs": json.loads(doc["kfs"]),
        "provenance": intake.provenance(doc),
    }


def _doc_view(doc: dict) -> dict:
    """A document as the intake page sees it: what was read, what is missing, what a
    human changed — plus a clause preview, so a reviewer can see what will be SPOKEN."""
    kfs = intake.kfs_of(doc)
    view = {
        "doc_id": doc["doc_id"],
        "filename": doc["filename"],
        "sha256": doc["sha256"],
        "media_type": doc["media_type"],
        "byte_size": doc["byte_size"],
        "method": doc["method"],
        "synthetic": doc["synthetic"],
        "uploaded_by_role": doc["uploaded_by_role"],
        "status": doc["status"],
        "fields": doc["report"].get("fields", []),
        "fee_rows": doc["report"].get("fee_rows", []),
        "fee_table_found": doc["report"].get("fee_table_found", False),
        "unknown_labels": doc["report"].get("unknown", {}),
        "validation_error": doc["report"].get("validation_error", ""),
        "missing": intake.missing_of(doc["report"], doc["corrections"]),
        "corrections": doc["corrections"],
        "clauses": [],
    }
    if kfs is not None:
        # Lazy: the API must be startable without the delivery stack present.
        from kfs.build_clauses import build_clauses  # noqa: PLC0415

        view["clauses"] = [
            {
                "clause_id": c.id,
                "title_hi": c.title_hi,
                "text": " ".join(s.text for s in c.segments),
                "key_values": [
                    {"kind": kv.kind, "value": str(kv.value), "raw_text": kv.raw_text,
                     "spoken_text": kv.spoken_text}
                    for kv in c.key_values
                ],
            }
            for c in build_clauses(kfs)
        ]
    return view


# ------------------------------------------------------------------------- livekit


@app.get("/calls/{call_id}/token")
def get_token(call_id: str, identity: str = "") -> dict:
    """Mint a room-scoped join token for the borrower's browser.

    The API secret never leaves the server. The grant is scoped to this one room, and the
    TTL is short: a call is minutes, not days.
    """
    url = os.environ.get("LIVEKIT_URL", "")
    key = os.environ.get("LIVEKIT_API_KEY", "")
    secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        raise HTTPException(
            503,
            "LiveKit is not configured. Set LIVEKIT_URL, LIVEKIT_API_KEY and "
            "LIVEKIT_API_SECRET in .env, then restart the server.",
        )

    # Lazy import: the API must be startable without the LiveKit stack present.
    from livekit.api import AccessToken, VideoGrants  # noqa: PLC0415

    who = identity or f"borrower-{call_id}"
    token = (
        AccessToken(key, secret)
        .with_identity(who)
        .with_name("borrower")
        .with_grants(VideoGrants(
            room_join=True, room=call_id,
            can_publish=True,        # her microphone, for teach-back
            can_subscribe=True,      # the agent's voice
            can_publish_data=True,   # the tap-to-interrupt affordance
        ))
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )
    return {"url": url, "token": token, "room": call_id, "identity": who}


@app.post("/calls/{call_id}/dispatch")
async def post_dispatch(call_id: str) -> dict:
    """Ask LiveKit to put the consent agent in this call's room.

    The agent worker registers with `agent_name`, which makes it EXPLICIT-dispatch only.
    That is deliberate: an automatically-dispatched worker joins every room in the LiveKit
    project — including rooms belonging to other apps sharing these credentials — and
    would have nothing sensible to read there.
    """
    call = store.get_call(conn(), call_id)
    if call is None:
        raise HTTPException(404, f"no call {call_id!r}")

    url = os.environ.get("LIVEKIT_URL", "")
    key = os.environ.get("LIVEKIT_API_KEY", "")
    secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        raise HTTPException(503, "LiveKit is not configured")

    from livekit import api as lkapi  # noqa: PLC0415

    rest = url.replace("wss://", "https://").replace("ws://", "http://")
    lk = lkapi.LiveKitAPI(rest, key, secret)
    try:
        dispatch = await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                room=call_id,
                agent_name=AGENT_NAME,
                # Pointers only. The agent loads the KFS from the store by id and
                # re-verifies the hash, so serving the wrong loan would require the
                # dispatch, the stored row and the bytes on disk to all agree.
                metadata=json.dumps({"v": 1, "call_id": call_id,
                                     "doc_id": call["doc_id"]}),
            )
        )
    except Exception as e:  # noqa: BLE001 — surface the vendor's message, do not swallow it
        raise HTTPException(502, f"LiveKit dispatch failed: {type(e).__name__}: {e}") from None
    finally:
        await lk.aclose()

    return {"call_id": call_id, "agent_name": AGENT_NAME, "dispatch_id": dispatch.id}


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
    return {
        "ok": True,
        "db": str(store.db_path()),
        "events": str(events.events_dir()),
        # The intake page renders the "real borrower data" option as disabled, with its
        # reason, rather than letting someone choose it and meet a 403 after picking a file.
        "allow_real_data": intake.allow_real_data(),
        "livekit_configured": bool(
            os.environ.get("LIVEKIT_URL")
            and os.environ.get("LIVEKIT_API_KEY")
            and os.environ.get("LIVEKIT_API_SECRET")
        ),
        "agent_name": AGENT_NAME,
    }


# ------------------------------------------------------------------------- pages
#
# Both routes serve the same file. The page reads location.pathname and shows either the
# live panel or the record viewer — one artifact to keep consistent instead of two.


def _page(name: str = "index.html") -> FileResponse:
    return FileResponse(WEB / name, media_type="text/html; charset=utf-8")


@app.get("/")
def index() -> FileResponse:
    return _page()


@app.get("/record/{call_id}")
def record_page(call_id: str) -> FileResponse:
    return _page()


# Three surfaces for three different people, because they are three different jobs.
# `/` is the judge's evidence panel. `/intake` is for whoever actually holds the PDF — a
# field officer or a branch clerk — and is unapologetically text-heavy. `/c/{call_id}` is
# the borrower's, and is near-textless: choosing a file off a filesystem is a
# literacy-heavy act, and she is not the one who does it.


@app.get("/intake")
def intake_page() -> FileResponse:
    return _page("intake.html")


@app.get("/c/{call_id}")
def borrower_page(call_id: str) -> FileResponse:
    return _page("call.html")


# Static assets, as explicit routes rather than a StaticFiles mount: `web/prompts/` does
# not exist until `make prompts` has been run, and mounting a missing directory fails at
# STARTUP — the whole API down because nobody synthesized the audio yet.

def _asset(kind: str, name: str, suffix: str) -> FileResponse:
    # A single path component with the expected extension. Explicit routes rather than a
    # catch-all `/{kind}/{name}`, which would sit in front of every other two-segment
    # path in the app.
    if name != Path(name).name or Path(name).suffix != suffix:
        raise HTTPException(404, "not found")
    path = WEB / kind / name
    if not path.is_file():
        raise HTTPException(404, f"no {kind}/{name}")
    return FileResponse(path)


@app.get("/vendor/{name}")
def vendor_asset(name: str) -> FileResponse:
    """The vendored livekit-client. Run `make vendor` if this 404s."""
    return _asset("vendor", name, ".js")


@app.get("/prompts/{name}")
def prompt_asset(name: str) -> FileResponse:
    """A pre-rendered Hindi prompt. A 404 here is not fatal: the borrower page falls back
    to the browser's own voice, so a demo without `make prompts` still speaks."""
    return _asset("prompts", name, ".wav")


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
    warned = False
    # A call created with a document can NEVER replay the fixture. api/demo.py reads
    # ₹1,25,000 at 18.5% regardless of what the uploaded document says, so replaying it
    # over a document-bound call would seal a record whose clauses describe a different
    # loan than its own provenance block names, sha256 and all. See store.allow_fallback.
    may_fall_back = store.allow_fallback(conn(), call_id)
    if call["mode"] == "demo" and store.event_count(conn(), call_id) == 0 and may_fall_back:
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
                if task is None and idle >= FALLBACK_AFTER_S and not warned:
                    warned = True
                    if may_fall_back:
                        ingest(call_id, events.normalize({
                            "type": "note",
                            "text": "No agent events on this call. Falling back to the "
                                    "built-in DEMO FIXTURE — this run is recorded as "
                                    "mode=demo and is a rehearsal, not a measurement.",
                        }))
                        task = asyncio.create_task(replay_demo(call_id))
                    else:
                        # Say so and wait. An empty record is the honest outcome; a
                        # fabricated one is not.
                        ingest(call_id, events.normalize({
                            "type": "note",
                            "text": "The consent agent has not joined this call. This call "
                                    "is bound to an uploaded document, so the demo fixture "
                                    "will NOT be replayed over it — the record stays "
                                    "PENDING. Start the agent worker (make agent) and "
                                    "dispatch it.",
                        }))
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
