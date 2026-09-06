"""The record store. stdlib sqlite3 — no ORM.

Five tables of substance plus one raw log. The typed tables exist because the consent
record has to be *defensible*: "which state was this clause in, when did it change, and
what did she say back" must be answerable row by row, not reconstructed by re-parsing a
blob. The raw `events` table exists for one job the typed tables cannot do — replaying the
call to a websocket client that connected late.

A REFUSAL IS A ROW, NOT AN ERROR. `consents.decision` is GRANTED or REFUSED and both are
written the same way. A schema in which refusal is an exception path is a schema that
loses the finding this product exists to produce.

`clause_id` is nullable on `consents`: a refusal can be attributed to the clause that
triggered it (the rushed-consent gate fires on a specific clause) or to the call as a
whole (the final decision). Both shapes occur in a real call.

Self-check:  uv run python -m api.store
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    ended_at    REAL,
    mode        TEXT NOT NULL DEFAULT 'live',   -- 'live' | 'demo'
    label       TEXT NOT NULL DEFAULT '',
    kfs_ref     TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '{}'      -- JSON: the active TTS/STT config
);

CREATE TABLE IF NOT EXISTS clauses (
    call_id          TEXT NOT NULL,
    clause_id        TEXT NOT NULL,
    ordinal          INTEGER NOT NULL DEFAULT 0,
    title_hi         TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'UNHEARD',
    heard_through_s  REAL NOT NULL DEFAULT 0,
    total_duration_s REAL NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 0,
    key_values       TEXT NOT NULL DEFAULT '[]',   -- JSON, incl. per-value `heard`
    PRIMARY KEY (call_id, clause_id)
);

CREATE TABLE IF NOT EXISTS transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id         TEXT NOT NULL,
    clause_id       TEXT NOT NULL,
    at              REAL NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    heard_through_s REAL NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS teachbacks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id    TEXT NOT NULL,
    clause_id  TEXT NOT NULL,
    at         REAL NOT NULL,
    attempt    INTEGER NOT NULL DEFAULT 1,
    transcript TEXT NOT NULL DEFAULT '',
    grade      TEXT NOT NULL DEFAULT '',
    passed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id              TEXT NOT NULL,
    clause_id            TEXT,               -- NULL = the decision for the call as a whole
    at                   REAL NOT NULL,
    decision             TEXT NOT NULL,      -- GRANTED | REFUSED | PENDING
    reason               TEXT NOT NULL DEFAULT '',
    flagged_for_callback INTEGER NOT NULL DEFAULT 0,
    blocking_clauses     TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    at      REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_call ON events(call_id, seq);
"""


def db_path() -> Path:
    """Read the env var per call, not at import, so tests can redirect it."""
    return Path(os.environ.get("SAMJHA_DB", "data/samjha.db"))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else db_path()
    if p.parent and str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI's threadpool may run a sync handler on any thread.
    # All writes go through one process-wide lock in api.main, so this is safe here.
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- calls


def create_call(conn: sqlite3.Connection, call_id: str, *, mode: str = "live",
                label: str = "", kfs_ref: str = "", provider: dict | None = None) -> dict:
    conn.execute(
        "INSERT OR IGNORE INTO calls(id, created_at, mode, label, kfs_ref, provider)"
        " VALUES (?,?,?,?,?,?)",
        (call_id, time.time(), mode, label, kfs_ref, json.dumps(provider or {}, ensure_ascii=False)),
    )
    conn.commit()
    return get_call(conn, call_id)


def get_call(conn: sqlite3.Connection, call_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["provider"] = json.loads(d["provider"] or "{}")
    return d


def set_mode(conn: sqlite3.Connection, call_id: str, mode: str) -> None:
    """Used when the live path degrades to the built-in fixture.

    The record must say so. A demo replay filed under mode='live' would be a fabricated
    measurement, which is the one thing this project cannot ship.
    """
    conn.execute("UPDATE calls SET mode=? WHERE id=?", (mode, call_id))
    conn.commit()


def list_calls(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT id, created_at, ended_at, mode, label, kfs_ref FROM calls"
        " ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- ingest


def apply_event(conn: sqlite3.Connection, call_id: str, ev: dict) -> int:
    """Fold one normalized event into the tables. Returns its sequence number.

    Idempotence is NOT attempted: the JSONL tailer tracks a byte offset and never replays
    a line, and the demo replay ingests each scripted event once. Adding dedup here would
    be a guess at a problem we do not have.
    """
    cur = conn.execute(
        "INSERT INTO events(call_id, at, payload) VALUES (?,?,?)",
        (call_id, ev.get("ts", time.time()), json.dumps(ev, ensure_ascii=False)),
    )
    seq = cur.lastrowid
    kind = ev.get("type")

    if kind == "call_start":
        conn.execute(
            "UPDATE calls SET label=COALESCE(NULLIF(?,''), label),"
            " kfs_ref=COALESCE(NULLIF(?,''), kfs_ref) WHERE id=?",
            (str(ev.get("label") or ""), str(ev.get("kfs_ref") or ""), call_id),
        )

    elif kind == "provider":
        provider = {k: v for k, v in ev.items() if k not in ("ts", "type")}
        conn.execute("UPDATE calls SET provider=? WHERE id=?",
                     (json.dumps(provider, ensure_ascii=False), call_id))

    elif kind in ("clause_registered", "clause_state"):
        _upsert_clause(conn, call_id, ev)
        if kind == "clause_state" and ev.get("state") != ev.get("prev_state"):
            conn.execute(
                "INSERT INTO transitions(call_id, clause_id, at, from_state, to_state,"
                " heard_through_s, reason) VALUES (?,?,?,?,?,?,?)",
                (call_id, str(ev.get("clause_id") or ""), ev.get("ts", time.time()),
                 ev.get("prev_state"), ev.get("state", "UNHEARD"),
                 float(ev.get("heard_through_s") or 0.0), str(ev.get("reason") or "")),
            )

    elif kind == "teach_back":
        conn.execute(
            "INSERT INTO teachbacks(call_id, clause_id, at, attempt, transcript, grade,"
            " passed) VALUES (?,?,?,?,?,?,?)",
            (call_id, str(ev.get("clause_id") or ""), ev.get("ts", time.time()),
             int(ev.get("attempt") or 1), str(ev.get("transcript") or ""),
             str(ev.get("grade") or ""), 1 if ev.get("passed") else 0),
        )

    elif kind == "consent":
        conn.execute(
            "INSERT INTO consents(call_id, clause_id, at, decision, reason,"
            " flagged_for_callback, blocking_clauses) VALUES (?,?,?,?,?,?,?)",
            (call_id, ev.get("clause_id"), ev.get("ts", time.time()),
             str(ev.get("decision") or "PENDING").upper(), str(ev.get("reason") or ""),
             1 if ev.get("flagged_for_callback") else 0,
             json.dumps(ev.get("blocking_clauses") or [], ensure_ascii=False)),
        )

    elif kind == "call_end":
        conn.execute("UPDATE calls SET ended_at=? WHERE id=?",
                     (ev.get("ts", time.time()), call_id))

    conn.commit()
    return seq


def _upsert_clause(conn: sqlite3.Connection, call_id: str, ev: dict) -> None:
    clause_id = str(ev.get("clause_id") or "")
    if not clause_id:
        return
    prev = conn.execute(
        "SELECT ordinal, title_hi FROM clauses WHERE call_id=? AND clause_id=?",
        (call_id, clause_id),
    ).fetchone()

    # An event may omit ordinal/title (a state change carries only what changed), so keep
    # whatever registration already established rather than blanking the board mid-demo.
    ordinal = ev.get("ordinal")
    if ordinal is None:
        ordinal = prev["ordinal"] if prev else _next_ordinal(conn, call_id)
    title = str(ev.get("title_hi") or "") or (prev["title_hi"] if prev else "")

    conn.execute(
        "INSERT INTO clauses(call_id, clause_id, ordinal, title_hi, state,"
        " heard_through_s, total_duration_s, active, key_values)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(call_id, clause_id) DO UPDATE SET"
        "  ordinal=excluded.ordinal, title_hi=excluded.title_hi, state=excluded.state,"
        "  heard_through_s=excluded.heard_through_s,"
        "  total_duration_s=excluded.total_duration_s, active=excluded.active,"
        "  key_values=excluded.key_values",
        (call_id, clause_id, int(ordinal), title, str(ev.get("state") or "UNHEARD"),
         float(ev.get("heard_through_s") or 0.0), float(ev.get("total_duration_s") or 0.0),
         1 if ev.get("active") else 0,
         json.dumps(ev.get("key_values") or [], ensure_ascii=False)),
    )


def _next_ordinal(conn: sqlite3.Connection, call_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) n FROM clauses WHERE call_id=?", (call_id,)).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------- reads


def clauses(conn: sqlite3.Connection, call_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM clauses WHERE call_id=? ORDER BY ordinal, clause_id", (call_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["key_values"] = json.loads(d["key_values"] or "[]")
        d["active"] = bool(d["active"])
        out.append(d)
    return out


def transitions(conn: sqlite3.Connection, call_id: str, clause_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM transitions WHERE call_id=?"
    args: list = [call_id]
    if clause_id is not None:
        sql += " AND clause_id=?"
        args.append(clause_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY at, id", args).fetchall()]


def teachbacks(conn: sqlite3.Connection, call_id: str, clause_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM teachbacks WHERE call_id=?"
    args: list = [call_id]
    if clause_id is not None:
        sql += " AND clause_id=?"
        args.append(clause_id)
    rows = conn.execute(sql + " ORDER BY at, id", args).fetchall()
    return [dict(r) | {"passed": bool(r["passed"])} for r in rows]


def consents(conn: sqlite3.Connection, call_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM consents WHERE call_id=? ORDER BY at, id", (call_id,)
    ).fetchall()
    return [
        dict(r) | {"flagged_for_callback": bool(r["flagged_for_callback"]),
                   "blocking_clauses": json.loads(r["blocking_clauses"] or "[]")}
        for r in rows
    ]


def events_since(conn: sqlite3.Connection, call_id: str, seq: int = 0,
                 limit: int = 2000) -> list[dict]:
    """Backlog for a websocket client that joined mid-call."""
    rows = conn.execute(
        "SELECT seq, payload FROM events WHERE call_id=? AND seq>? ORDER BY seq LIMIT ?",
        (call_id, seq, limit),
    ).fetchall()
    return [json.loads(r["payload"]) | {"_seq": r["seq"]} for r in rows]


def event_count(conn: sqlite3.Connection, call_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) n FROM events WHERE call_id=?",
                            (call_id,)).fetchone()["n"])


def _demo() -> None:
    """Self-check on an in-memory DB: a refusal survives as a first-class row."""
    conn = connect(":memory:")
    create_call(conn, "c1", label="self-check")

    apply_event(conn, "c1", {"ts": 1.0, "type": "clause_registered", "clause_id": "acct",
                             "title_hi": "ऋण खाता संख्या", "ordinal": 0,
                             "total_duration_s": 12.4, "state": "UNHEARD"})
    apply_event(conn, "c1", {"ts": 2.0, "type": "clause_state", "clause_id": "acct",
                             "prev_state": "UNHEARD", "state": "PARTIALLY_HEARD",
                             "heard_through_s": 8.4, "total_duration_s": 12.4,
                             "reason": "barge-in mid-number",
                             "key_values": [{"kind": "account_identifier",
                                             "raw_text": "000512348899", "heard": False}]})
    apply_event(conn, "c1", {"ts": 3.0, "type": "consent", "clause_id": "acct",
                             "decision": "refused", "reason": "rushed consent",
                             "flagged_for_callback": True, "blocking_clauses": ["acct"]})

    cl = clauses(conn, "c1")
    assert len(cl) == 1 and cl[0]["state"] == "PARTIALLY_HEARD"
    assert cl[0]["title_hi"] == "ऋण खाता संख्या"  # survived the title-less state event
    assert cl[0]["key_values"][0]["heard"] is False

    tr = transitions(conn, "c1")
    assert len(tr) == 1 and tr[0]["to_state"] == "PARTIALLY_HEARD"

    co = consents(conn, "c1")
    assert len(co) == 1 and co[0]["decision"] == "REFUSED"
    assert co[0]["flagged_for_callback"] is True

    assert event_count(conn, "c1") == 3
    assert len(events_since(conn, "c1", 1)) == 2

    print("store OK — refusal recorded as a row, not raised as an error")


if __name__ == "__main__":
    _demo()
