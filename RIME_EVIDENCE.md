# RIME_EVIDENCE

Rime integration, the claim we tested, how we tested it, what we found, and what we do not
know. Every number here is reproducible with `make eval` from committed artifacts.

**This is not a benchmark project.** We do not compare Rime to other TTS providers. Both
arms of every experiment are Rime, with identical request parameters. The variable is our
own text layer.

---

## 1. Rime configuration

Rime Coda is the **primary and only spoken output** in the judged flow.

| Field | Value |
|---|---|
| `modelId` | `coda` — pinned explicitly on every request |
| `speaker` | `taru` (Hindi, male) |
| `lang` | `hi` |
| `audioFormat` / `samplingRate` | `mulaw` / `8000` |
| `timeScaleFactor` | `1.0`, held constant. `speedAlpha` is never sent. |
| Transport | `wss://users-ws.rime.ai/ws3`, `segment=never` + explicit flush |
| Endpoint region | US (Rime has no India region) |

`make preflight` fetches the live catalog and hard-fails if the `(modelId, speaker, lang)`
triple is absent. No speaker list is hardcoded anywhere in the repo.

**Why the explicit pin matters:** `modelId` defaults to `mistv3`, which serves no Hindi at
all, and Rime's docs state that an unsupported pairing "can be accepted and synthesized"
rather than erroring. A single typo would have silently routed the entire experiment to the
wrong model while still returning working audio.

---

## 2. The claim

Pre-registered in [`evals/ACCEPTANCE.md`](evals/ACCEPTANCE.md), committed **before any
product code existed** (commit `d55ac24`, 2026-09-05 21:50:03 IST).

> On a Hindi-language, 8 kHz μ-law telephone-grade channel, SAMJHA's delivery layer causes
> comprehension-critical financial values to be recovered exactly at a materially higher
> rate than the same clause text sent to Rime unprocessed, with model, speaker, lang,
> sampling rate and endpoint held constant.

**Day-1 pilot partially refuted this, and the claim was narrowed the same night** —
before the agent, the harness or the UI existed. The amendment is logged in ACCEPTANCE.md
§10 rather than edited into the original claim.

---

## 3. Result

24 utterances × 2 arms × 2 scorers = 96 scored rows. Smoke run.

### Overall Value Error Rate

VER = % of comprehension-critical values not recovered exactly, scored on **parsed typed
values**, never strings.

| scorer | baseline | delivery layer | delta |
|---|---|---|---|
| Sarvam | 12.5% | **0.0%** | +12.5 pp |
| Deepgram | 16.7% | **4.2%** | +12.5 pp |

### Per category — this is the deliverable

| category | scorer | baseline | delivery | delta | n |
|---|---|---|---|---|---|
| **account_identifier** | deepgram | **100.0%** | **0.0%** | **+100.0 pp** | 4 |
| **account_identifier** | sarvam | **75.0%** | **0.0%** | **+75.0 pp** | 4 |
| rupee_amount | sarvam | 0.0% | 0.0% | 0.0 pp | 4 |
| rupee_amount | deepgram | 0.0% | 25.0% | **−25.0 pp** | 4 |
| percentage_apr | both | 0.0% | 0.0% | 0.0 pp | 4 |
| emi_amount | both | 0.0% | 0.0% | 0.0 pp | 4 |
| tenure_months | both | 0.0% | 0.0% | 0.0 pp | 4 |
| date_deadline | both | 0.0% | 0.0% | 0.0 pp | 4 |

**Four of six categories show no effect, and we report them.** Rime Coda handles Indian
digit grouping natively: `₹1,04,596`, `104596` and our verbalised form all produced
identical transcripts, with `₹` correctly rendered as रुपये. Percentages, tenures and EMI
amounts were correct raw. For those categories our delivery layer adds nothing measurable,
and **Rime's own documentation — "most applications shouldn't pre-normalize" — was right.**

**The one regression is honest and is an ASR artifact, not a delivery failure.** The single
`rupee_amount` miss: we sent `एक लाख तिरेसठ हज़ार आठ सौ` (163800); Deepgram transcribed
`एक लाख छियासठ हज़ार आठ सौ` (166800). तिरेसठ/छियासठ are phonetically close. Sarvam recovered
the same clip correctly. n=1 of 4 — noise, reported rather than dropped.

### Where the delivery layer earns its place

Digit-sequence identifiers are read by Coda as Indian-scale **quantities** rather than
sequences. Measured failure modes in the baseline arm:

| account number | baseline outcome |
|---|---|
| `9157114007` | spoken as `नौ अरब पंद्रह करोड़ इकहत्तर लाख चौदह हज़ार सात` — "nine billion…" |
| `402011000521` | digits dropped → `4020110521` |
| `000512348899` | **leading zeros lost**, recovered as `95058000000` |
| `1234500067` | not recovered at all |
| `50100247865321` | digits dropped → `501247865321` |

Delivery layer: **5/5 exact**, leading zeros intact.

For a consent product this is not cosmetic. A borrower asked to confirm her loan account
number, who hears a nine-billion-rupee quantity, has not been informed of anything.

---

## 4. The ITN artifact — why ASR recovery overstates comprehension

A digit sequence that matches **only** on Sarvam's `transcribe` pass may have been *spoken*
as a quantity and reconstructed into digits by the provider's inverse text normalization.
By ASR it scores as recovered. By human comprehension it is not.

| arm | matched on transcribe only | matched on verbatim | not recovered |
|---|---|---|---|
| baseline | 1 | 0 | 3 |
| delivery layer | 4 | 0 | 0 |

That single baseline "success" is the artifact: `9157114007` scored recovered while what
was actually said was `नौ अरब पंद्रह करोड़…`. **By ASR the baseline arm scores 1/4; by
comprehension it is plausibly 0/4.**

This is exactly the failure mode SP-MCQA (arXiv:2510.26190, 2025) documents — that ASR WER
is a poor proxy for whether a listener recovers key information, and that TTS fails
specifically at text normalization. It appeared in our own pilot data on day 1, and it is
why the human panel is load-bearing rather than confirmatory.

### Inter-scorer agreement

Two scorers that agree on everything are one measurement. Reported instead of quietly
taking the better number.

- pairs compared: 48 · observed agreement **95.8%** · Cohen's κ **0.729**
- recovery rate: Sarvam 93.8%, Deepgram 89.6%

---

## 5. Three Rime findings we measured rather than assumed

**Word-level timestamps do not exist for Hindi, and fail silently.** Docs: emitted "only
when `lang` is `en`/`eng` or `es`/`spa`, or when `lang` is omitted… no `timestamps` event
and no error." We tested the omitted-`lang` loophole. It *does* emit timestamps with
correct Devanagari tokens — but the values are **byte-identical across sample rates**
(4.51325 at both 8 kHz μ-law and 22.05 kHz PCM) while audio duration is not (3.13 s vs
3.69 s), at a non-constant ratio (1.223–1.489). They do not describe the delivered stream.
Unusable. Full data in [`evals/results/battery/FINDINGS.md`](evals/results/battery/FINDINGS.md).

So heard-through uses **our own flush-segment boundaries and byte-count duration** —
μ-law at 8 kHz is 1 byte per sample, so 8000 bytes = 1.000 s exactly — cross-checked
against LiveKit's measured `PlaybackFinishedEvent.playback_position`. Vendor-independent,
language-independent, exact.

**There is no cancel primitive.** `clear` does not cancel in-flight synthesis, and
`contextId` is not a fence — Rime "does not maintain multiple simultaneous context IDs",
and it returned `null` on every frame we observed. Barge-in therefore stops playout, sends
`clear`, and **closes the socket**, with a warm spare connection kept open so reconnect
latency never lands on the borrower.

**The Rime LiveKit plugin cannot produce the telephony channel.** `plugins/rime/tts.py`
hardcodes `"audioFormat": "pcm"`, so 8 kHz μ-law is unreachable through it. Clause audio
therefore goes through our own `/ws3` client and is handed to `session.say(text, audio=…)`.
The plugin remains configured (`use_websocket=True`, model pinned to `coda`) for the
agent's conversational turns.

---

## 6. Limitations

Written before the results existed, in ACCEPTANCE.md §8. None removed after the fact.

- **This smoke run is n=24 utterances, 4 per category.** A single item moves a category by
  25 points. Treat per-category deltas as directional; only `account_identifier` is large
  enough to survive that.
- **ASR error is not human misunderstanding.** Both are reported. The human panel
  (3 native speakers, blind, 30 clips) is specified in ACCEPTANCE.md §7 and **has not yet
  been run** — until it is, every comprehension claim here rests on a proxy.
- **The telephone channel is simulated**, not a live PSTN leg.
- **"Heard" means played out of the speaker**, ± the client jitter buffer. We measure
  playout position, not cognition.
- Coda serves exactly two Hindi voices, so voice-level variance cannot be averaged over.
  Speaker held constant at `taru` — a limitation, not a control.
- SNR is computed from whole-file RMS, not ITU-T P.56 active speech level. Applied
  identically to both arms.
- The Hindi clause text has not been reviewed by a native speaker.
- All data synthetic. No real borrower data.

---

## 7. Reproduce

```bash
make preflight   # live catalog check — must pass
make eval        # A/B, both scorers, per-category table
```

Artifacts: `evals/results/item_level.csv` (one row per utterance × arm × scorer),
`run_config.json` (exact request parameters, endpoint, versions), `summary.md`, every clip
under `evals/results/`, and the day-1 battery under `evals/results/battery/`.

Cached and fresh counts are reported separately in every run.
