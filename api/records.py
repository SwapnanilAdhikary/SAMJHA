"""The consent record, and the hash that makes it defensible.

What a record has to survive is a dispute: months later, someone claims the borrower was
never told the APR. The record must answer, per clause, (1) what state it ended in,
(2) how much of it was actually played out of the speaker, (3) WHICH key values completed
— not which were sent — (4) what she said back and how it was graded, and (5) when each of
those happened. Anything less is a receipt, not evidence.

THE HASH IS OVER A CANONICAL SERIALISATION, and the envelope carrying it is not part of
the input. `generated_at` therefore lives outside the hashed payload: a record that hashes
differently every time you look at it cannot be used to prove anything.

Canonicalisation: json.dumps(sort_keys=True, separators=(',',':'), ensure_ascii=False),
UTF-8. ensure_ascii=False matters here — the record is full of Devanagari, and escaping it
to \\uXXXX would make the bytes depend on a serialiser flag rather than on the content.

A REFUSAL IS A RESULT. `consent.decision == "REFUSED"` is a completed record, not a failed
one, and `refusals` is a top-level field so no reader can miss it.

Self-check:  uv run python -m api.records
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from api import store

RECORD_VERSION = 1
HASH_ALGORITHM = "sha256"
CANONICALIZATION = (
    "json.dumps(sort_keys=True, separators=(',',':'), ensure_ascii=False).encode('utf-8')"
)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="milliseconds")


def canonical_json(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def record_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def build_record(conn: sqlite3.Connection, call_id: str) -> dict:
    """The hashed payload. Deterministic: same rows in, same bytes out."""
    call = store.get_call(conn, call_id)
    if call is None:
        raise KeyError(call_id)

    clause_rows = store.clauses(conn, call_id)
    all_tb = store.teachbacks(conn, call_id)
    all_tr = store.transitions(conn, call_id)

    clauses = []
    for c in clause_rows:
        total = float(c["total_duration_s"] or 0.0)
        heard = float(c["heard_through_s"] or 0.0)
        kvs = c["key_values"]
        clauses.append({
            "clause_id": c["clause_id"],
            "ordinal": c["ordinal"],
            "title_hi": c["title_hi"],
            "final_state": c["state"],
            "heard_through_s": round(heard, 3),
            "total_duration_s": round(total, 3),
            # The number a regulator reads first. 0 when nothing was ever delivered —
            # never 100, because "no playout event" means heard NOTHING (LiveKit drops the
            # chat item entirely if an interrupt lands before the first frame).
            "heard_through_pct": round(100.0 * heard / total, 1) if total > 0 else 0.0,
            "key_values": [_kv(k) for k in kvs],
            "key_values_heard": [_kv(k) for k in kvs if k.get("heard")],
            "key_values_not_heard": [_kv(k) for k in kvs if not k.get("heard")],
            "all_key_values_heard": bool(kvs) and all(k.get("heard") for k in kvs),
            "transitions": [
                {"at": iso(t["at"]), "from": t["from_state"], "to": t["to_state"],
                 "heard_through_s": round(float(t["heard_through_s"] or 0.0), 3),
                 "reason": t["reason"]}
                for t in all_tr if t["clause_id"] == c["clause_id"]
            ],
            "teach_back": [
                {"at": iso(t["at"]), "attempt": t["attempt"], "transcript": t["transcript"],
                 "grade": t["grade"], "passed": t["passed"]}
                for t in all_tb if t["clause_id"] == c["clause_id"]
            ],
        })

    decisions = [
        {"at": iso(d["at"]), "clause_id": d["clause_id"], "decision": d["decision"],
         "reason": d["reason"], "flagged_for_callback": d["flagged_for_callback"],
         "blocking_clauses": d["blocking_clauses"]}
        for d in store.consents(conn, call_id)
    ]
    refusals = [d for d in decisions if d["decision"] == "REFUSED"]

    # UNDERSTOOD is the only state that permits consent (kfs.clauses.ClauseState).
    blocking = [c["clause_id"] for c in clauses if c["final_state"] != "UNDERSTOOD"]

    final = decisions[-1] if decisions else None
    return {
        "record_version": RECORD_VERSION,
        "call_id": call_id,
        "mode": call["mode"],
        "synthetic_data": True,  # every KFS in this project is synthetic. Always.
        "label": call["label"],
        "kfs_ref": call["kfs_ref"],
        "started_at": iso(call["created_at"]),
        "ended_at": iso(call["ended_at"]),
        "provider": call["provider"],
        "clauses": clauses,
        "consent": final or {"decision": "PENDING", "at": None, "clause_id": None,
                             "reason": "", "flagged_for_callback": False,
                             "blocking_clauses": blocking},
        "consent_decisions": decisions,
        "refusals": refusals,
        "consent_permitted": bool(clauses) and not blocking,
        "clauses_blocking_consent": blocking,
    }


def _kv(k: dict) -> dict:
    """Only the fields that belong in evidence — display flags are not evidence."""
    out = {
        "kind": k.get("kind", "unknown"),
        "raw_text": k.get("raw_text", ""),
        "spoken_text": k.get("spoken_text", ""),
        "heard": bool(k.get("heard")),
    }
    if k.get("segment_end_s") is not None:
        out["segment_end_s"] = round(float(k["segment_end_s"]), 3)
    if k.get("value") is not None:
        out["value"] = str(k["value"])
    return out


def sealed_record(conn: sqlite3.Connection, call_id: str) -> dict:
    """The record plus its seal. `generated_at` is OUTSIDE the hashed payload."""
    payload = build_record(conn, call_id)
    return {
        "hash": record_hash(payload),
        "hash_algorithm": HASH_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "generated_at": iso(datetime.now(UTC).timestamp()),
        "record": payload,
    }


def _demo() -> None:
    """Self-check: hash stability, and that a refusal is recorded rather than raised."""
    conn = store.connect(":memory:")
    store.create_call(conn, "c1", label="self-check", kfs_ref="SYNTH-KFS-001")

    store.apply_event(conn, "c1", {"ts": 1.0, "type": "clause_registered",
                                   "clause_id": "acct", "ordinal": 0,
                                   "title_hi": "ऋण खाता संख्या", "state": "UNHEARD",
                                   "total_duration_s": 12.4})
    store.apply_event(conn, "c1", {"ts": 2.0, "type": "clause_state", "clause_id": "acct",
                                   "prev_state": "UNHEARD", "state": "PARTIALLY_HEARD",
                                   "heard_through_s": 8.4, "total_duration_s": 12.4,
                                   "reason": "barge-in mid-number",
                                   "key_values": [{"kind": "account_identifier",
                                                   "raw_text": "000512348899",
                                                   "segment_end_s": 9.1, "heard": False}]})
    store.apply_event(conn, "c1", {"ts": 3.0, "type": "consent", "decision": "REFUSED",
                                   "reason": "clause not understood",
                                   "flagged_for_callback": True})

    rec = build_record(conn, "c1")
    h1 = record_hash(rec)
    assert record_hash(build_record(conn, "c1")) == h1, "hash must be stable"

    c = rec["clauses"][0]
    assert c["final_state"] == "PARTIALLY_HEARD"
    assert c["heard_through_pct"] == 67.7
    assert c["key_values_not_heard"][0]["raw_text"] == "000512348899"
    assert rec["consent"]["decision"] == "REFUSED" and rec["refusals"]
    assert rec["consent_permitted"] is False

    # One more second of playout is a different record and therefore a different hash.
    store.apply_event(conn, "c1", {"ts": 4.0, "type": "clause_state", "clause_id": "acct",
                                   "prev_state": "PARTIALLY_HEARD", "state": "HEARD",
                                   "heard_through_s": 12.4, "total_duration_s": 12.4,
                                   "key_values": [{"kind": "account_identifier",
                                                   "raw_text": "000512348899",
                                                   "segment_end_s": 9.1}]})
    assert record_hash(build_record(conn, "c1")) != h1

    sealed = sealed_record(conn, "c1")
    assert sealed["hash"] == record_hash(sealed["record"])
    assert "generated_at" not in sealed["record"]  # or the hash would drift every read

    print(f"record OK — sha256 {h1[:16]}…, refusal recorded, hash stable")


if __name__ == "__main__":
    _demo()
