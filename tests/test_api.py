"""Record construction, hash stability, refusal recording, and that the API serves.

The hash tests are the ones that matter. A consent record whose digest drifts between
reads cannot be used to defend or dispute a loan, so "same input, same hash" is not a
nicety — it is the property the whole record is for.

No live server: FastAPI's TestClient drives the ASGI app in-process. No network, no Rime,
no LiveKit, no LiveKit secret (which is a masked placeholder on this machine anyway).
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from api import demo, events, main, provider, records, store


@pytest.fixture
def tmpenv(tmp_path, monkeypatch):
    """Redirect the DB and the event directory. Both read their env var per call."""
    monkeypatch.setenv("SAMJHA_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SAMJHA_EVENTS_DIR", str(tmp_path / "events"))
    monkeypatch.setattr(main, "_conn", None)
    main._offsets.clear()
    return tmp_path


@pytest.fixture
def client(tmpenv):
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def conn(tmpenv):
    return store.connect()


def seed(conn, call_id: str, script) -> None:
    for ev in script:
        store.apply_event(conn, call_id, events.normalize(ev))


REFUSAL_SCRIPT = [
    {"ts": 1.0, "type": "clause_registered", "clause_id": "acct", "ordinal": 0,
     "title_hi": "ऋण खाता संख्या", "state": "UNHEARD", "total_duration_s": 12.4},
    {"ts": 2.0, "type": "clause_state", "clause_id": "acct", "prev_state": "UNHEARD",
     "state": "PARTIALLY_HEARD", "heard_through_s": 8.4, "total_duration_s": 12.4,
     "reason": "barge-in mid-number",
     "key_values": [{"kind": "account_identifier", "raw_text": "000512348899",
                     "spoken_text": "शून्य शून्य शून्य पाँच ...", "segment_end_s": 9.1}]},
    {"ts": 3.0, "type": "consent", "clause_id": "acct", "decision": "refused",
     "reason": "rushed consent", "flagged_for_callback": True,
     "blocking_clauses": ["acct"]},
]


# ------------------------------------------------------------------ the record


def test_record_captures_what_was_actually_heard(conn):
    store.create_call(conn, "c1", label="t", kfs_ref="SYNTH-KFS-001")
    seed(conn, "c1", REFUSAL_SCRIPT)

    rec = records.build_record(conn, "c1")
    c = rec["clauses"][0]

    assert c["final_state"] == "PARTIALLY_HEARD"
    assert c["heard_through_pct"] == 67.7
    # The segment carrying the account number ends at 9.1 s and only 8.4 s played.
    # A value cut off halfway was not communicated: it must not count as heard.
    assert c["key_values_heard"] == []
    assert c["key_values_not_heard"][0]["raw_text"] == "000512348899"
    assert c["all_key_values_heard"] is False
    assert c["transitions"][0]["reason"] == "barge-in mid-number"
    assert rec["synthetic_data"] is True


def test_refusal_is_a_record_not_an_error(conn):
    store.create_call(conn, "c1")
    seed(conn, "c1", REFUSAL_SCRIPT)

    rec = records.build_record(conn, "c1")
    assert rec["consent"]["decision"] == "REFUSED"
    assert rec["consent"]["flagged_for_callback"] is True
    assert len(rec["refusals"]) == 1
    assert rec["refusals"][0]["clause_id"] == "acct"
    assert rec["consent_permitted"] is False
    assert rec["clauses_blocking_consent"] == ["acct"]

    # And it survives to the wire as a completed 200, never an exception.
    assert store.consents(conn, "c1")[0]["decision"] == "REFUSED"


def test_lowercase_decision_is_normalized(conn):
    """The agent writes "refused"; a viewer filtering on "REFUSED" must still see it."""
    store.create_call(conn, "c1")
    seed(conn, "c1", REFUSAL_SCRIPT)
    assert records.build_record(conn, "c1")["refusals"]


# -------------------------------------------------------------------- the hash


def test_hash_is_stable_across_reads(conn):
    store.create_call(conn, "c1")
    seed(conn, "c1", REFUSAL_SCRIPT)
    h = records.record_hash(records.build_record(conn, "c1"))
    assert records.record_hash(records.build_record(conn, "c1")) == h
    assert len(h) == 64


def test_hash_changes_when_the_record_changes(conn):
    store.create_call(conn, "c1")
    seed(conn, "c1", REFUSAL_SCRIPT)
    before = records.record_hash(records.build_record(conn, "c1"))

    seed(conn, "c1", [{"ts": 4.0, "type": "clause_state", "clause_id": "acct",
                       "prev_state": "PARTIALLY_HEARD", "state": "HEARD",
                       "heard_through_s": 12.4, "total_duration_s": 12.4,
                       "key_values": [{"kind": "account_identifier",
                                       "raw_text": "000512348899",
                                       "segment_end_s": 9.1}]}])
    assert records.record_hash(records.build_record(conn, "c1")) != before


def test_hash_covers_the_heard_ledger(conn):
    """Two calls identical except for one second of playout must not share a digest."""
    def build(played: float) -> str:
        c = store.connect(":memory:")
        store.create_call(c, "x")
        seed(c, "x", [{"ts": 1.0, "type": "clause_state", "clause_id": "acct",
                       "state": "PARTIALLY_HEARD", "heard_through_s": played,
                       "total_duration_s": 12.4,
                       "key_values": [{"kind": "account_identifier",
                                       "raw_text": "000512348899",
                                       "segment_end_s": 9.1}]}])
        return records.record_hash(records.build_record(c, "x"))

    assert build(8.4) != build(9.4)


def test_generated_at_is_outside_the_hashed_payload(conn):
    store.create_call(conn, "c1")
    seed(conn, "c1", REFUSAL_SCRIPT)
    sealed = records.sealed_record(conn, "c1")
    assert "generated_at" not in sealed["record"]
    assert sealed["hash"] == records.record_hash(sealed["record"])
    assert records.sealed_record(conn, "c1")["hash"] == sealed["hash"]


def test_canonical_json_keeps_devanagari_unescaped(conn):
    """ensure_ascii=False: the bytes must depend on the content, not a serialiser flag."""
    raw = records.canonical_json({"title_hi": "ऋण खाता संख्या"})
    assert "ऋण".encode() in raw
    assert b"\\u" not in raw


# ---------------------------------------------------------------------- HTTP


def test_page_is_served_at_both_routes(client):
    for path in ("/", "/record/anything"):
        r = client.get(path)
        assert r.status_code == 200
        assert "SAMJHA" in r.text
        # The panel is the artifact; if these regions are gone the demo shows nothing.
        for marker in ("LIVE CLAUSE STATE", "CONSENT BLOCKED", "PARTIALLY_HEARD"):
            assert marker in r.text.upper()


def test_create_and_fetch_a_call(client):
    call = client.post("/calls", json={"label": "test", "kfs_ref": "SYNTH-1"}).json()
    got = client.get(f"/calls/{call['id']}").json()
    assert got["label"] == "test"
    assert got["active_provider"]["tts"]["model_id"] == "coda"
    assert client.get("/calls/nope").status_code == 404


def test_record_endpoint_returns_the_sealed_record(client):
    cid = client.post("/calls", json={}).json()["id"]
    for ev in REFUSAL_SCRIPT:
        events.emit(cid, **{k: v for k, v in ev.items() if k != "ts"})

    body = client.get(f"/calls/{cid}/record").json()
    assert body["hash_algorithm"] == "sha256"
    assert body["hash"] == records.record_hash(body["record"])
    assert body["record"]["refusals"], "the refusal must reach the wire"
    assert client.get("/calls/nope/record").status_code == 404


def test_provider_badge_data_comes_from_the_delivery_constants(client):
    p = client.get("/provider").json()
    assert (p["tts"]["model_id"], p["tts"]["speaker"], p["tts"]["lang"]) == \
        ("coda", "taru", "hi")
    assert p["tts"]["sample_rate"] == 8000 and p["tts"]["audio_format"] == "mulaw"
    assert "PSTN" in p["channel"]          # the limitation must be on screen
    assert "Deepgram" in p["fallback"]     # fallback behaviour must be disclosed


def test_demo_endpoint_creates_a_call_marked_demo(client):
    body = client.post("/demo").json()
    assert body["mode"] == "demo" and body["duration_s"] > 0
    assert client.get(f"/calls/{body['call_id']}").json()["mode"] == "demo"


# --------------------------------------------------------------- JSONL tailing


def test_tailer_folds_agent_jsonl_and_ignores_partial_lines(client, tmpenv):
    cid = client.post("/calls", json={}).json()["id"]
    p = events.path_for(cid)
    p.parent.mkdir(parents=True, exist_ok=True)

    # The shape agent/consent_fsm.py writes: `kind`, ISO `at`, played_s/clause_duration_s.
    line = json.dumps({
        "kind": "transition", "call_id": cid, "clause_id": "acct",
        "from_state": "UNHEARD", "to_state": "PARTIALLY_HEARD",
        "reason": "interrupted_before_key_value_completed",
        "at": "2026-09-06T18:30:00+00:00", "monotonic_s": 12.5,
        "played_s": 8.4, "clause_duration_s": 12.4,
        "heard_key_values": [], "unheard_key_values": ["account_identifier=000512348899"],
        "detail": {"interrupted": True},
    }, ensure_ascii=False)

    # A half-written line must be left for the next poll, not parsed and dropped.
    p.write_text(line + "\n" + line[:40], encoding="utf-8")
    assert main.drain_jsonl(cid) == 1
    assert main.drain_jsonl(cid) == 0

    with p.open("a", encoding="utf-8") as f:
        f.write(line[40:] + "\n")
    assert main.drain_jsonl(cid) == 1

    got = client.get(f"/calls/{cid}").json()["clauses"]
    assert got[0]["state"] == "PARTIALLY_HEARD"
    assert got[0]["heard_through_s"] == 8.4
    assert got[0]["key_values"][0]["raw_text"] == "000512348899"
    assert got[0]["key_values"][0]["heard"] is False


def test_a_malformed_line_degrades_the_panel_rather_than_crashing_it(client):
    cid = client.post("/calls", json={}).json()["id"]
    p = events.path_for(cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"nope\n{"type":"note","text":"still here"}\n', encoding="utf-8")
    assert main.drain_jsonl(cid) == 1
    assert client.get(f"/calls/{cid}/record").status_code == 200


def test_missing_jsonl_is_not_an_error(client):
    cid = client.post("/calls", json={}).json()["id"]
    assert main.drain_jsonl(cid) == 0
    assert client.get(f"/calls/{cid}/record").json()["record"]["clauses"] == []


# --------------------------------------------------------------- the fixture


def test_demo_fixture_replays_through_the_real_ingest_path(conn):
    """The rehearsal must produce a real, hashed record — and end in a refusal."""
    store.create_call(conn, "d", mode="demo")
    seed(conn, "d", [ev for _, ev in demo.script()])

    rec = records.build_record(conn, "d")
    by_id = {c["clause_id"]: c for c in rec["clauses"]}
    assert rec["mode"] == "demo"
    assert by_id["loan_account"]["final_state"] == "UNDERSTOOD"
    assert by_id["cooling_off"]["final_state"] == "PARTIALLY_HEARD"
    assert rec["consent"]["decision"] == "REFUSED"
    assert rec["consent"]["flagged_for_callback"] is True
    assert len(records.record_hash(rec)) == 64

    # Every state the panel can show is exercised, or the demo cannot rehearse it.
    seen = {ev["state"] for _, ev in demo.script() if ev["type"] == "clause_state"}
    assert seen == set(events.CLAUSE_STATES)


def test_demo_fixture_carries_no_fabricated_measurements():
    """No latency, VER or WER figure may appear in a scripted call."""
    blob = json.dumps(demo.script(), ensure_ascii=False).lower()
    for word in ("ttfb", "p50", "p95", "wer", "ver", "latency", "benchmark"):
        assert not re.search(rf"\b{word}\b", blob), \
            f"fixture leaks a measurement-shaped claim: {word!r}"


def test_websocket_pushes_transitions(client, tmpenv):
    cid = client.post("/calls", json={}).json()["id"]
    with client.websocket_connect(f"/calls/{cid}/live") as ws:
        assert ws.receive_json()["type"] == "hello"
        events.emit(cid, "clause_state", clause_id="acct", prev_state="UNHEARD",
                    state="PARTIALLY_HEARD", heard_through_s=8.4, total_duration_s=12.4,
                    key_values=[{"kind": "account_identifier", "raw_text": "0005",
                                 "segment_end_s": 9.1}])
        ev = ws.receive_json()
        assert ev["type"] == "clause_state" and ev["state"] == "PARTIALLY_HEARD"
        assert ev["key_values"][0]["heard"] is False


def test_provider_dict_is_json_serialisable():
    json.dumps(provider.active())
