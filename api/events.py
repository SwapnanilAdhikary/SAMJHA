"""The append-only event log: the seam between the agent and the UI.

The agent appends one JSON object per line to `events/{call_id}.jsonl`; the API tails that
file, folds each event into SQLite, and rebroadcasts it over a websocket. A file rather
than a socket because it is also the artifact Pathway would read (PLAN.md), it survives a
crashed API, and a demo can be replayed from it hours later.

CANONICAL EVENT SHAPE — the agent writes these, everything else reads them:

    {"ts": 1757000000.0, "type": "call_start",       "kfs_ref": "...", "label": "..."}
    {"ts": ..., "type": "provider",   "tts": {...}, "stt": {...}, "fallback": "..."}
    {"ts": ..., "type": "clause_registered", "clause_id": "apr", "title_hi": "...",
                "ordinal": 2, "total_duration_s": 9.5, "key_values": [...]}
    {"ts": ..., "type": "clause_state", "clause_id": "apr",
                "prev_state": "UNHEARD", "state": "PARTIALLY_HEARD",
                "heard_through_s": 8.4, "total_duration_s": 12.4,
                "active": false, "reason": "barge-in mid-number",
                "key_values": [{"kind": "account_identifier", "raw_text": "0005...",
                                "spoken_text": "शून्य ...", "segment_end_s": 9.1,
                                "heard": false}]}
    {"ts": ..., "type": "teach_back", "clause_id": "apr", "attempt": 1,
                "transcript": "...", "grade": "fail", "passed": false}
    {"ts": ..., "type": "consent", "clause_id": null, "decision": "REFUSED",
                "reason": "...", "flagged_for_callback": true,
                "blocking_clauses": ["cooling_off"]}
    {"ts": ..., "type": "call_end"}
    {"ts": ..., "type": "note", "text": "..."}   # free-form, shown in the ticker

`active` is deliberately NOT an FSM state. ClauseState has exactly five members and
"currently being spoken" is not one of them; conflating the two would corrupt the record.
It is a display flag that lets the heard-through bar animate during delivery.

`normalize()` is forgiving on purpose. This file is written by a different process, under
time pressure, and a malformed line must degrade the UI, never crash the API mid-demo.

Self-check:  uv run python -m api.events
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

EVENT_TYPES = (
    "call_start",
    "provider",
    "clause_registered",
    "clause_state",
    "teach_back",
    "consent",
    "call_end",
    "note",
)

CLAUSE_STATES = (
    "UNHEARD",
    "PARTIALLY_HEARD",
    "HEARD",
    "TEACH_BACK_FAIL",
    "UNDERSTOOD",
)


def events_dir() -> Path:
    """Read the env var per call, not at import, so tests can redirect it."""
    return Path(os.environ.get("SAMJHA_EVENTS_DIR", "events"))


def path_for(call_id: str) -> Path:
    return events_dir() / f"{call_id}.jsonl"


def emit(call_id: str, type: str, **fields) -> dict:
    """Append one event. The agent calls this; nothing else should."""
    ev = {"ts": time.time(), "type": type, **fields}
    p = path_for(call_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        f.flush()  # the UI tails this file; block buffering would stall the demo by 4 KB
    return ev


# Field aliases we accept, because the writer and the reader are being built in parallel
# and a name mismatch at 2 am should not be a blank screen.
_ALIASES = {
    "clause_id": ("clause_id", "clause", "id"),
    "state": ("state", "to_state", "to"),
    "prev_state": ("prev_state", "from_state", "from"),
    "ts": ("ts", "timestamp", "t", "at"),
    "type": ("type", "event", "kind"),
}


def _first(raw: dict, key: str, default=None):
    for name in _ALIASES[key]:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


def normalize_key_value(kv) -> dict:
    """Accept a dict, or a bare string (treated as raw_text of an unknown kind)."""
    if not isinstance(kv, dict):
        return {"kind": "unknown", "raw_text": str(kv), "spoken_text": "",
                "segment_end_s": None, "heard": False}
    out = {
        "kind": str(kv.get("kind") or "unknown"),
        "raw_text": str(kv.get("raw_text") or kv.get("text") or ""),
        "spoken_text": str(kv.get("spoken_text") or ""),
        "segment_end_s": kv.get("segment_end_s"),
        "heard": bool(kv.get("heard", False)),
    }
    if "value" in kv and kv["value"] is not None:
        out["value"] = str(kv["value"])
    return out


def normalize(raw: dict, *, heard_through_s: float | None = None) -> dict:
    """Coerce one logged line into the canonical shape. Never raises on bad input."""
    if not isinstance(raw, dict):
        return {"ts": time.time(), "type": "note", "text": str(raw)[:500]}

    ev = dict(raw)
    ev["ts"] = _ts(_first(raw, "ts"))
    ev["type"] = str(_first(raw, "type", "note"))

    cid = _first(raw, "clause_id")
    if cid is not None:
        ev["clause_id"] = str(cid)

    if ev["type"] in ("clause_state", "clause_registered"):
        state = _first(raw, "state", "UNHEARD")
        ev["state"] = state if state in CLAUSE_STATES else "UNHEARD"
        prev = _first(raw, "prev_state")
        ev["prev_state"] = prev if prev in CLAUSE_STATES else None
        ev["heard_through_s"] = _num(raw.get("heard_through_s"), 0.0)
        ev["total_duration_s"] = _num(raw.get("total_duration_s"), 0.0)
        ev["active"] = bool(raw.get("active", False))
        ev["key_values"] = [normalize_key_value(k) for k in raw.get("key_values") or []]

        # A value counts as heard only if the ENTIRE segment carrying it finished
        # playing. Derive it when the writer gave us the boundary but not the verdict —
        # the same rule as kfs.clauses.Clause.heard_key_values, which is the authority.
        # A segment_end_s of 0 means the segment carries no audio — it was never
        # synthesized — so it cannot have been heard. Without that guard `0 <= 0 + 1e-9`
        # marks the value heard on a clause that never played. Same rule as
        # kfs.clauses.Clause.heard_key_values, which is the authority.
        played = ev["heard_through_s"] if heard_through_s is None else heard_through_s
        for kv in ev["key_values"]:
            if kv["segment_end_s"] is not None and not kv["heard"]:
                end = float(kv["segment_end_s"])
                kv["heard"] = end > 0.0 and end <= played + 1e-9

    if ev["type"] == "consent":
        ev["decision"] = str(raw.get("decision") or "PENDING").upper()
        ev["flagged_for_callback"] = bool(raw.get("flagged_for_callback", False))

    if ev["type"] == "teach_back":
        ev["passed"] = bool(raw.get("passed", False))
        ev["transcript"] = str(raw.get("transcript") or "")
        ev["grade"] = str(raw.get("grade") or ("pass" if ev["passed"] else "fail"))

    return ev


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ts(v) -> float:
    """Epoch seconds from a float OR an ISO 8601 string.

    agent/consent_fsm.py timestamps with `datetime.now(UTC).isoformat()`, so a bare
    float() here would raise on every line the agent writes.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            pass
    return time.time()


# --------------------------------------------------------------- FSM shape adapter
#
# agent/consent_fsm.py emits its own vocabulary: `kind` rather than `type`, one
# `transition` row carrying both the state change and the teach-back that caused it, and
# key values rendered as "kind=raw_text" labels so the row stays readable years later
# without this codebase. Rather than ask that workstream to restate the record it owns,
# the ingest side translates. Canonical lines (`type`) pass through untouched.


def _kv_from_label(label: str, heard: bool) -> dict:
    kind, _, raw = str(label).partition("=")
    return normalize_key_value({"kind": kind or "unknown", "raw_text": raw, "heard": heard})


def _from_fsm(raw: dict) -> list[dict]:
    kind = raw.get("kind")
    ts = _ts(raw.get("at"))

    if kind == "call_started":
        out = [{"ts": ts, "type": "call_start", "label": str(raw.get("label") or ""),
                "kfs_ref": str(raw.get("kfs_ref") or "")}]
        # `call_started` names the clauses and their read order. Registering them now is
        # what lets the panel show the whole KFS greyed out before delivery begins.
        out += [{"ts": ts, "type": "clause_registered", "clause_id": cid, "ordinal": i,
                 "state": "UNHEARD"} for i, cid in enumerate(raw.get("clauses") or [])]
        return out

    if kind == "transition":
        detail = raw.get("detail") or {}
        reason = str(raw.get("reason") or "")
        ev = {
            "ts": ts, "type": "clause_state", "clause_id": raw.get("clause_id"),
            "prev_state": raw.get("from_state"), "state": raw.get("to_state"),
            "heard_through_s": raw.get("played_s"),
            "total_duration_s": raw.get("clause_duration_s"),
            "reason": reason,
            # A delivery that has only just started is still playing; anything else is a
            # settled outcome. `active` is a display flag, never an FSM state.
            "active": reason in ("deliver", "redelivery_from_start"),
            "key_values": [_kv_from_label(s, True) for s in raw.get("heard_key_values") or []]
            + [_kv_from_label(s, False) for s in raw.get("unheard_key_values") or []],
        }
        out = [ev]
        if detail.get("transcript") is not None:
            out.append({"ts": ts, "type": "teach_back", "clause_id": raw.get("clause_id"),
                        "attempt": detail.get("attempt", 1),
                        "transcript": detail["transcript"], "grade": reason,
                        "passed": raw.get("to_state") == "UNDERSTOOD"})
        return out

    if kind == "consent_decision":
        # `agent/rushed_consent.ConsentDecision` names these fields `granted`,
        # `human_callback` and `reasons`; the store and the record read `decision`,
        # `flagged_for_callback` and `reason`. Passing the dict through unchanged left
        # every REAL call's decision to fall back to "PENDING" with the callback flag
        # clear — so a refusal, the finding this product exists to produce, silently
        # became "no decision recorded". The demo fixture hid it by writing the canonical
        # names directly. Translating here is exactly this adapter's job.
        drop = ("kind", "at", "monotonic_s", "call_id", "granted", "human_callback",
                "reasons")
        reasons = raw.get("reasons") or []
        ev = {
            "ts": ts,
            "type": "consent",
            **{k: v for k, v in raw.items() if k not in drop},
            "decision": "GRANTED" if raw.get("granted") else "REFUSED",
            "flagged_for_callback": bool(raw.get("human_callback")),
            # Kept as a list too: the record shows the reasons individually, and joining
            # is only for the one-line `reason` column.
            "reasons": list(reasons),
            "reason": "; ".join(str(r) for r in reasons),
        }
        return [ev]

    return [{"ts": ts, "type": "note", "text": json.dumps(raw, ensure_ascii=False)[:500]}]


def expand(raw) -> list[dict]:
    """One logged line -> zero or more canonical events. Never raises."""
    if isinstance(raw, dict) and "type" not in raw and "kind" in raw:
        return [normalize(e) for e in _from_fsm(raw)]
    return [normalize(raw)]


def clause_payload(clause) -> dict:
    """A `kfs.clauses.Clause` -> the `clause_state` event body.

    Lives here so the agent never has to hand-build this dict and drift from the schema.
    Imported lazily: the API must be startable without the delivery stack present.
    """
    # Delegate the verdict to kfs.clauses.Clause, which is the authority on it, rather
    # than repeating the arithmetic. Repeating it is how this function came to report a
    # value as heard for a clause with no audio at all: `0.0 <= 0.0 + 1e-9` is true, so a
    # clause registered before delivery — or one whose TTS failed — claimed every value
    # had been heard. Identity comparison, because two KeyValues can be equal by value.
    heard = {id(kv) for kv in clause.heard_key_values()}

    kvs = []
    for i, seg in enumerate(clause.segments):
        if seg.key_value is None:
            continue
        end = round(clause.segment_end_time(i), 3)
        kv = seg.key_value
        kvs.append({
            "kind": kv.kind,
            "value": str(kv.value),
            "raw_text": kv.raw_text,
            "spoken_text": kv.spoken_text,
            "segment_end_s": end,
            "heard": id(kv) in heard,
        })
    return {
        "clause_id": clause.id,
        "title_hi": clause.title_hi,
        "state": clause.state.value,
        "heard_through_s": round(clause.heard_through_s, 3),
        "total_duration_s": round(clause.total_duration_s, 3),
        "key_values": kvs,
    }


def read_all(call_id: str) -> list[dict]:
    """Every event logged for a call. Bad lines are skipped, not fatal."""
    p = path_for(call_id)
    if not p.exists():
        return []
    out = []
    for line in p.read_bytes().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.extend(expand(json.loads(line)))
        except (ValueError, UnicodeDecodeError):
            continue
    return out


def _demo() -> None:
    """Self-check: the alias tolerance and the heard-derivation rule."""
    ev = normalize({"t": 1.0, "event": "clause_state", "clause": "apr", "to": "HEARD",
                    "from": "UNHEARD", "heard_through_s": 9.5, "total_duration_s": 9.5,
                    "key_values": [{"kind": "percentage_apr", "raw_text": "18.5%",
                                    "segment_end_s": 5.5}]})
    assert ev["clause_id"] == "apr" and ev["state"] == "HEARD"
    assert ev["prev_state"] == "UNHEARD" and ev["ts"] == 1.0
    assert ev["key_values"][0]["heard"] is True

    # Cut off before the value's segment ended: NOT heard. The whole product in one line.
    cut = normalize({"type": "clause_state", "clause_id": "acct", "state": "PARTIALLY_HEARD",
                     "heard_through_s": 8.4, "total_duration_s": 12.4,
                     "key_values": [{"kind": "account_identifier", "segment_end_s": 9.1}]})
    assert cut["key_values"][0]["heard"] is False

    assert normalize("garbage")["type"] == "note"
    assert normalize({"type": "clause_state", "state": "NONSENSE"})["state"] == "UNHEARD"

    # The shape agent/consent_fsm.py actually writes, ISO timestamp and all.
    fsm = expand({
        "kind": "transition", "call_id": "c", "clause_id": "acct",
        "from_state": "UNHEARD", "to_state": "PARTIALLY_HEARD",
        "reason": "interrupted_before_key_value_completed",
        "at": "2026-09-06T18:30:00.000+00:00", "monotonic_s": 12.5,
        "played_s": 8.4, "clause_duration_s": 12.4,
        "heard_key_values": [], "unheard_key_values": ["account_identifier=000512348899"],
        "detail": {"interrupted": True},
    })
    assert len(fsm) == 1
    assert fsm[0]["type"] == "clause_state" and fsm[0]["state"] == "PARTIALLY_HEARD"
    assert fsm[0]["heard_through_s"] == 8.4 and fsm[0]["total_duration_s"] == 12.4
    assert fsm[0]["key_values"][0]["raw_text"] == "000512348899"
    assert fsm[0]["key_values"][0]["heard"] is False
    assert fsm[0]["ts"] > 1_700_000_000  # the ISO string parsed, not fell back to now()

    # One transition carrying a teach-back becomes two canonical events.
    pair = expand({"kind": "transition", "clause_id": "acct", "from_state": "HEARD",
                   "to_state": "UNDERSTOOD", "reason": "teach_back_pass", "at": 1.0,
                   "played_s": 12.4, "clause_duration_s": 12.4,
                   "detail": {"transcript": "शून्य शून्य ...", "attempt": 2}})
    assert [e["type"] for e in pair] == ["clause_state", "teach_back"]
    assert pair[1]["passed"] is True and pair[1]["attempt"] == 2

    started = expand({"kind": "call_started", "call_id": "c", "clauses": ["a", "b"],
                      "at": "2026-09-06T18:29:00+00:00"})
    assert [e["type"] for e in started] == ["call_start", "clause_registered",
                                            "clause_registered"]

    from kfs.clauses import Clause, KeyValue, Segment

    kv = KeyValue("account_identifier", "000512348899", "000512348899", "शून्य शून्य ...")
    c = Clause(id="acct", title_hi="ऋण खाता संख्या", heard_through_s=2.5, segments=[
        Segment(text="आपका खाता नंबर", audio=b"\x00" * 8000),
        Segment(text="शून्य शून्य ...", key_value=kv, audio=b"\x00" * 16000),
    ])
    p = clause_payload(c)
    assert p["key_values"][0]["heard"] is False and p["total_duration_s"] == 3.0
    c.heard_through_s = 3.0
    assert clause_payload(c)["key_values"][0]["heard"] is True

    print("event normalization OK")


if __name__ == "__main__":
    _demo()
