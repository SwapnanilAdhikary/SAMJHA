# SAMJHA (समझा) — voice-native informed consent for Indian retail lending

> **Status: end to end.** KFS pipeline, consent agent, eval harness and web UI all built;
> 337 tests pass. Measured results are in [`RIME_EVIDENCE.md`](RIME_EVIDENCE.md), against a
> test pre-registered in [`evals/ACCEPTANCE.md`](evals/ACCEPTANCE.md) *before any product
> code existed*. Not yet done: the human listening panel, and a live PSTN leg (out of
> scope — the channel is simulated).

**Headline result.** Value Error Rate on account identifiers, 8 kHz μ-law channel,
model/speaker/lang held constant: **100% → 0%** (Deepgram), **75% → 0%** (Sarvam).
Four of six categories show *no* effect and are reported as the negative result they are —
Rime Coda handles Indian digit grouping, percentages and tenures correctly on its own.

RBI mandates a Key Facts Statement for every retail loan, in a language the borrower
understands, with informed acknowledgement. In practice lenders email a PDF and collect an
OTP. A borrower with low reading fluency — sold a loan over a phone call, where there is
no screen — acknowledges a document she cannot read.

SAMJHA reads the KFS aloud in Hindi, clause by clause; tracks which comprehension-critical
values she actually **heard** before interrupting; requires her to explain the key terms
back in her own words; and emits a consent record made of audio.

**Remove speech and the product does not degrade — it ceases to exist.**

---

## Quick start

```bash
make setup          # uv sync against pinned deps
cp .env.example .env   # then add your keys

make preflight      # verify the voice triple against Rime's LIVE catalog
make test           # 337 tests
make channel        # self-check the telephone-channel chain
make demo-fixtures  # the two stress cases, no network needed
make secrets        # fail if a credential ever touched git history

make eval           # the A/B (smoke run; --full for all 120 utterances)
uv run uvicorn api.main:app --port 8000   # then open /?demo=1
```

---

## The one hard claim

Coda exposes **no** phoneme control, **no** SSML, **no** pause tags and **no** lexicon —
and Hindi exists on no other Rime model. There is no fallback model with those knobs.
**The text layer is the only lever there is.** That is not a convenient framing; it is the
documented parameter surface.

The claim under test, and the acceptance criteria, are pre-registered in
[`evals/ACCEPTANCE.md`](evals/ACCEPTANCE.md), committed before any product code.

## Rime configuration

| Field | Value |
|---|---|
| `modelId` | `coda` — **pinned explicitly** |
| `speaker` | `taru` (Hindi, male). `nadi` is the only alternative. |
| `lang` | `hi` |
| `audioFormat` / `samplingRate` | `mulaw` / `8000` |
| `timeScaleFactor` | `1.0`, held constant. `speedAlpha` is never sent. |
| Transport | `wss://users-ws.rime.ai/ws3`, `segment=never` + explicit flush |

`modelId` **defaults to `mistv3`**, which has no Hindi at all, and Rime's docs state that
an unsupported pairing "can be accepted and synthesized" rather than erroring. So a single
typo would silently route the whole experiment to the wrong model while still returning
working audio. `make preflight` fetches the live catalog and hard-fails on a bad triple;
no speaker list is hardcoded anywhere.

## Two design decisions worth explaining

**We do not use Rime's word timestamps.** They are emitted only for English and Spanish —
a Hindi request receives `chunk` and `done` with no `timestamps` event *and no error*.
Instead the normaliser emits **at most one key value per flush segment**, and heard-through
is derived from LiveKit's real `playback_position` over known segment durations. mu-law at
8 kHz is 1 byte per sample, so byte count converts to duration exactly. This works in any
language and removes a vendor dependency from the central claim.

**We do not fence barge-in with `contextId`.** Rime has no cancel primitive — `clear` does
not cancel in-flight synthesis, and Rime "does not maintain multiple simultaneous context
IDs", so naive contextId-discard mis-attributes audio. A hard stop closes the socket, with
a warm spare connection kept open so reconnect latency never lands on the borrower.

## Limitations

Stated before the results exist, not after.

- The telephone channel is **simulated**. No live PSTN leg has been validated.
- "Heard" means *played out of the speaker*, ± the client jitter buffer. We measure
  playout position, not cognition.
- Coda serves exactly two Hindi voices, so voice-level variance cannot be averaged over.
  Speaker is held constant — that is a limitation, not a control.
- **Not a TTS benchmark.** We do not compare Rime to other providers. Both arms are Rime.
- All data is synthetic. No real borrower data, ever.

## Layout

```
delivery/   normalizer/hindi.py · rime_ws3.py · config_guard.py
kfs/        schema.py (RBI Annex A) · build_clauses.py · extract.py · finance.py
agent/      consent_fsm.py · teachback.py · rushed_consent.py · session.py · main.py
evals/      ACCEPTANCE.md (pre-registered) · run_eval.py · asr_score.py · channel.py
            corpus/ (120 typed utterances) · results/ · human_panel/
api/        main.py · store.py · records.py (hashed consent record)
web/        index.html — clause state, provider badge, record viewer, ?demo=1
fixtures/   synthetic/ — 10 KFS documents + PDFs. All synthetic.
scripts/    battery.py (day-1 experiments) · smoke.py (credential checks)
```
