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
    provider    TEXT NOT NULL DEFAULT '{}',     -- JSON: the active TTS/STT config
    doc_id      TEXT NOT NULL DEFAULT '',       -- documents.doc_id; '' = no document
    -- 0 forever once a call carries a document. There is deliberately NO setter: see
    -- allow_fallback() below.
    fallback_allowed INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id           TEXT PRIMARY KEY,
    created_at       REAL NOT NULL,
    filename         TEXT NOT NULL DEFAULT '',
    sha256           TEXT NOT NULL,
    media_type       TEXT NOT NULL DEFAULT '',
    byte_size        INTEGER NOT NULL DEFAULT 0,
    -- How the facts were obtained: 'pdf_table' | 'docx_table'. Named, not inferred, so a
    -- consent record can state its own provenance. A future 'vision' reader lands here.
    method           TEXT NOT NULL DEFAULT '',
    -- An UPLOADER ASSERTION, never a determination. Guessing whether a document contains
    -- a real person's data is exactly the guess this product must not make.
    synthetic        INTEGER NOT NULL DEFAULT 1,
    uploaded_by_role TEXT NOT NULL DEFAULT '',  -- lender_system | branch_helper | borrower
    status           TEXT NOT NULL DEFAULT 'draft',  -- 'draft' | 'confirmed'
    kfs              TEXT,                      -- KFS.model_dump_json(), NULL until complete
    report           TEXT NOT NULL DEFAULT '{}',     -- JSON: per-field read status
    corrections      TEXT NOT NULL DEFAULT '[]'      -- JSON: [{label,value,by,at}]
);
CREATE INDEX IF NOT EXISTS documents_by_sha ON documents(sha256);

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
    _migrate(conn)
    return conn


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing database, so a DB written before document upload existed keeps its old `calls`
# shape and every SELECT naming a new column fails. Adding them here means an existing
# data/samjha.db keeps working instead of having to be deleted.
_ADDED_COLUMNS = {
    "calls": (
        ("doc_id", "TEXT NOT NULL DEFAULT ''"),
        ("fallback_allowed", "INTEGER NOT NULL DEFAULT 1"),
    ),
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


# --------------------------------------------------------------------------- calls


def create_call(conn: sqlite3.Connection, call_id: str, *, mode: str = "live",
                label: str = "", kfs_ref: str = "", provider: dict | None = None,
                doc_id: str = "") -> dict:
    """Create a call row.

    A call that carries a document has `fallback_allowed = 0` from birth, and nothing can
    set it back. Replaying `api/demo.py`'s scripted fixture over a call bound to a real
    uploaded loan would produce a sha256-sealed consent record whose clauses describe a
    DIFFERENT loan than its own provenance block names — the fixture reads ₹1,25,000 at
    18.5% no matter what the document says. The fixture path stays for bare call ids,
    which is what it was built for.
    """
    conn.execute(
        "INSERT OR IGNORE INTO calls(id, created_at, mode, label, kfs_ref, provider,"
        " doc_id, fallback_allowed) VALUES (?,?,?,?,?,?,?,?)",
        (call_id, time.time(), mode, label, kfs_ref,
         json.dumps(provider or {}, ensure_ascii=False), doc_id, 0 if doc_id else 1),
    )
    conn.commit()
    return get_call(conn, call_id)


def allow_fallback(conn: sqlite3.Connection, call_id: str) -> bool:
    """May this call degrade to the built-in demo fixture?

    Read-only on purpose. There is no `set_fallback_allowed`: the answer is fixed by
    whether the call was created with a document, so no later code path can talk itself
    into replaying a fixture over a real borrower's loan.
    """
    row = conn.execute("SELECT fallback_allowed FROM calls WHERE id=?", (call_id,)).fetchone()
    return bool(row["fallback_allowed"]) if row is not None else True


# ----------------------------------------------------------------------- documents


def put_document(conn: sqlite3.Connection, doc_id: str, *, sha256: str, filename: str = "",
                 media_type: str = "", byte_size: int = 0, method: str = "",
                 synthetic: bool = True, uploaded_by_role: str = "",
                 kfs_json: str | None = None, report: dict | None = None,
                 corrections: list | None = None, status: str = "draft") -> dict:
    conn.execute(
        "INSERT OR REPLACE INTO documents(doc_id, created_at, filename, sha256, media_type,"
        " byte_size, method, synthetic, uploaded_by_role, status, kfs, report, corrections)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, time.time(), filename, sha256, media_type, byte_size, method,
         1 if synthetic else 0, uploaded_by_role, status, kfs_json,
         json.dumps(report or {}, ensure_ascii=False),
         json.dumps(corrections or [], ensure_ascii=False)),
    )
    conn.commit()
    return get_document(conn, doc_id)


def get_document(conn: sqlite3.Connection, doc_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["synthetic"] = bool(d["synthetic"])
    d["report"] = json.loads(d["report"] or "{}")
    d["corrections"] = json.loads(d["corrections"] or "[]")
    return d


def update_document(conn: sqlite3.Connection, doc_id: str, **fields) -> dict | None:
    """Patch named columns. JSON-shaped values are encoded here, not by the caller."""
    if not fields:
        return get_document(conn, doc_id)

    allowed = {"status", "kfs", "report", "corrections", "synthetic", "uploaded_by_role",
               "method", "filename"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update {sorted(unknown)} on a document")

    sets, values = [], []
    for key, value in fields.items():
        sets.append(f"{key}=?")
        if key in ("report", "corrections"):
            values.append(json.dumps(value, ensure_ascii=False))
        elif key == "synthetic":
            values.append(1 if value else 0)
        else:
            values.append(value)
    values.append(doc_id)

    conn.execute(f"UPDATE documents SET {', '.join(sets)} WHERE doc_id=?", values)
    conn.commit()
    return get_document(conn, doc_id)


def list_documents(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT doc_id, created_at, filename, sha256, media_type, method, synthetic,"
        " uploaded_by_role, status FROM documents ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["synthetic"] = bool(d["synthetic"])
        out.append(d)
    return out


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

    # ---- documents round-trip, provenance intact
    doc = put_document(
        conn, "abc123-000001", sha256="f" * 64, filename="kfs.pdf",
        media_type="application/pdf", byte_size=4096, method="pdf_table",
        synthetic=True, uploaded_by_role="branch_helper",
        kfs_json='{"proposal_no":"PL/1"}',
        report={"fields": [{"label": "Cooling-off period (days)", "status": "parsed"}]},
    )
    assert doc["sha256"] == "f" * 64 and doc["method"] == "pdf_table"
    assert doc["synthetic"] is True and doc["status"] == "draft"
    assert doc["report"]["fields"][0]["status"] == "parsed"
    assert get_document(conn, "nope") is None

    doc = update_document(conn, "abc123-000001", status="confirmed",
                          corrections=[{"label": "Recovery agents", "value": "x"}])
    assert doc["status"] == "confirmed" and doc["corrections"][0]["label"] == "Recovery agents"
    assert len(list_documents(conn)) == 1
    try:
        update_document(conn, "abc123-000001", sha256="0" * 64)  # provenance is not patchable
    except ValueError as e:
        assert "sha256" in str(e)
    else:
        raise AssertionError("sha256 must not be updatable — it is the document's identity")

    # ---- THE fallback rule. A call with a document can never replay the demo fixture,
    # whose clauses would describe a different loan than the record's own provenance.
    create_call(conn, "bare")
    create_call(conn, "bound", doc_id="abc123-000001")
    assert allow_fallback(conn, "bare") is True
    assert allow_fallback(conn, "bound") is False
    assert get_call(conn, "bound")["doc_id"] == "abc123-000001"
    # No setter exists to undo it. Asserted rather than merely commented, so adding one
    # later trips this self-check instead of quietly reopening the hole.
    assert "set_fallback_allowed" not in globals()
    # An unknown call defaults to allowed: that is the bare-call-id path, not a document.
    assert allow_fallback(conn, "never-created") is True

    print("store OK — refusal is a row; a document-bound call cannot replay the fixture")


if __name__ == "__main__":
    _demo()
