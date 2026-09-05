# SAMJHA — Pre-registered Acceptance Test

**Committed before any product code exists. The git timestamp on this file is the evidence that the test
preceded the result.** Nothing in this document may be edited after the first eval run except the
"Amendments" section at the bottom, which records what changed and why.

---

## 1. The claim under test

> On a Hindi-language, 8 kHz μ-law telephone-grade channel, SAMJHA's **delivery layer** (Indic normalization +
> one-value-per-segment chunking) causes comprehension-critical financial values — ₹ amounts in Indian digit
> grouping, percentages, tenures, EMI amounts and account identifiers — to be recovered **exactly** at a
> materially higher rate than the same clause text sent to Rime unprocessed, with `modelId`, `speaker`, `lang`,
> `samplingRate`, `timeScaleFactor` and endpoint held constant.

Condition B is the **delivery layer as a whole**, not the normalizer in isolation. Chunking is required by the
consent mechanic regardless, so the two are measured together. This is stated deliberately, not to inflate the
effect.

**This is not a benchmark project.** We do not compare Rime to other TTS providers. Both arms are Rime.

---

## 2. Prediction (stated before measurement)

We predict condition B reduces Value Error Rate relative to condition A, with the largest effect on
`account_identifier` and `rupee_amount`, and the smallest on `tenure_months`.

**We commit to reporting the result either way.** If VER(B) ≥ VER(A), that is the finding and it goes in
`RIME_EVIDENCE.md` under this same heading.

---

## 3. Corpus

- 10 synthetic RBI Key Facts Statement documents (`evals/corpus/kfs_docs/`). **All data synthetic. No real
  borrower data, ever.**
- 120 utterances (`evals/corpus/utterances.json`), 20 per category:

| Category | Example value | Intended reading |
|---|---|---|
| `rupee_amount` | ₹1,25,000 | cardinal |
| `percentage_apr` | 18.5% | cardinal + प्रतिशत |
| `tenure_months` | 36 महीने | cardinal |
| `emi_amount` | ₹4,382 | cardinal |
| `account_identifier` | 9157114007 | digit-by-digit |
| `date_deadline` | 15 मार्च 2026 | date |

- Each value is embedded in a **native Hindi carrier sentence** written by a native speaker, not translated.
- Ground truth is stored as a **typed value**, never a string:
  `{"id": "...", "type": "rupee_amount", "value": 125000, "intended_reading": "cardinal", "carrier_hi": "..."}`
- `intended_reading` is recorded so that a failure caused by the model reading a digit run as a cardinal
  quantity can be separated from a failure caused by the 8 kHz channel. These are different defects.

---

## 4. Conditions

Exactly one thing differs between arms: the text sent to Rime.

| Held constant | Value |
|---|---|
| `modelId` | `coda` (pinned explicitly on every request) |
| `speaker` | `taru` |
| `lang` | `hi` |
| `samplingRate` | 8000 |
| `audioFormat` | `mulaw` |
| `timeScaleFactor` | 1.0 (`speedAlpha` is never sent) |
| Transport | `wss://users-ws.rime.ai/ws3` |
| Endpoint region | recorded at run time, reported |
| Chunk boundaries | identical across arms |

- **A — Baseline:** clause text → Rime, unprocessed.
- **B — SAMJHA:** clause text → Indic normalizer → chunker (≤1 key value per segment) → Rime.

`make preflight` must pass before any run: it fetches the live Rime catalog and hard-fails if the
`(modelId, speaker, lang)` triple is absent. A run whose preflight did not pass is discarded.

---

## 5. Channel simulation

Fixed order of operations, held constant across both arms:

```
synthesize → resample 8 kHz → band-limit 300–3400 Hz → add noise at target SNR
           → G.711 μ-law encode → μ-law decode → ASR
```

```bash
ffmpeg -y -i clean.wav -af "aresample=8000,highpass=f=300:p=2,lowpass=f=3400:p=2" \
       -c:a pcm_mulaw -ar 8000 -ac 1 tel_ulaw.wav
ffmpeg -y -i tel_ulaw.wav -c:a pcm_s16le -ar 8000 tel_pcm.wav
```

Noise is added **before** the codec (simulating a noisy handset, not line noise). One frozen noise file,
SNR points {20, 10, 5} dB. Noise source and SNR method are recorded in `results/run_config.json`.

⚠️ Known deviation from best practice, disclosed: SNR is computed from whole-file RMS, **not** ITU-T P.56
active speech level. A clause with long pauses is therefore measured as quieter than it is, and receives
slightly less noise than the nominal SNR implies. Applied identically to both arms.

---

## 6. Scoring

**Primary metric — Value Error Rate (VER):** the percentage of comprehension-critical values not recovered
exactly, scored on the **parsed typed value**, not on string equality.

Transcript → value extraction handles, explicitly:
- Devanagari digits (U+0966–U+096F) mapped to ASCII. **NFKC does not do this.**
- Indic multipliers: हज़ार 10³, लाख 10⁵, करोड़ 10⁷, अरब 10⁹.
- Colloquial collapse: डेढ़ 1.5, ढाई 2.5, सव्वा 1.25, साढ़े +0.5, पौने −0.25.
- Latin tokens inside Devanagari output (notably the English word "zero" inside a spelled-out digit run).
- Both दशमलव and the English "point" as decimal separators.

**Dual-pass ASR to remove the numeral-format confound.** Every clip is transcribed twice by Sarvam Saaras
against the same audio: `mode="transcribe"` (returns Arabic digits) and `mode="verbatim"` (returns spoken
number words). A value counts as recovered if **either** pass yields an exact match after normalization.
Rationale: providers differ in inverse-text-normalization behaviour, and a digit-only regex would score a
spelled-out transcript at ~0% on every item — an artifact, not a result.

**Second independent scorer.** Deepgram `nova-3`, `language="hi"` (never `multi`). We report **inter-scorer
agreement**, not just the better number.

**Secondary metrics:** per-category VER · WER · TTFB reported **warm and cold separately** · barge-in stop
latency P50/P95.

---

## 7. Human panel

3 native Hindi speakers, none of them authors of this project, blind to condition, transcribing 30 clips
presented in randomized order. **Labelled exploratory.**

ASR is a proxy for human comprehension; the panel is the honest check on that proxy. **Where the panel and the
ASR scorers disagree, the disagreement is reported, not resolved in our favour.**

Project authors are excluded from the panel. An author who knows which arm is which is not a blind listener.

---

## 8. Stated limitations

Written before the result is known.

- ASR error is not human misunderstanding. Both are reported.
- n = 120 utterances, 3 listeners. Exploratory, not a benchmark.
- One accent register, one noise profile, one SNR sweep.
- Rime Coda exposes exactly two Hindi voices, so voice-level variance cannot be averaged over. Speaker is held
  constant at `taru` and this is a limitation, not a control.
- "Heard" means *played out of the speaker*, ± the client jitter buffer. We measure playout position, not
  cognition.
- The telephone channel is **simulated**. No live PSTN leg was validated.
- Endpoint region is US; measurements from India include trans-Pacific RTT. Network latency is reported
  separately from model latency.
- Rime's own documentation states that most applications should not pre-normalize, carving out an exception for
  "regulatory disclosures, legal read-backs, or confirmation flows". Our claim is scoped to exactly that
  exception and does not generalize.

---

## 9. Reproduction

```
make preflight   # live catalog check — must pass
make eval        # full A/B, one command
```

Committed artifacts: `corpus/utterances.json`, every generated clip under `results/clips/{baseline,samjha}/`,
`results/item_level.csv` (one row per utterance per arm per scorer), `results/run_config.json` (exact request
parameters, endpoint, versions), `results/summary.md`, and the human panel sheets under `human_panel/`.

**Every performance number that appears in the README must be reproducible from these artifacts.**

---

## 10. Amendments

Any change after the first eval run is logged here with date, what changed, and why.

### 2026-09-05, ~22:30 IST — claim narrowed to digit-sequence identifiers

**The day-1 pilot partially refuted the original claim, and this is reported as a result,
not edited away.** The pivot was pre-staged in the build plan before the test ran.

**What was refuted.** Rime Coda handles Indian digit grouping natively. `₹1,04,596`,
`104596` and our verbalised form produced *identical* transcripts —
`एक लाख चार हज़ार पाँच सौ छियानवे रुपये` — with `₹` correctly rendered as रुपये. Percentages
(`18.5%` → `18.5 प्रतिशत`) and tenures (`36 महीने`) were also correct raw. **For these
categories the delivery layer adds nothing measurable, and the original claim does not
hold.** This is consistent with Rime's own guidance that most applications should not
pre-normalize.

**What survived, and strengthened.** Digit-sequence identifiers are read as Indian-scale
*quantities*, not sequences. Pilot, n=5 account numbers, exact recovery of the full digit
string:

| Arm | Exact recovery |
|---|---|
| A — raw | **1/5** |
| B — delivery layer | **5/5** |

Observed raw failure modes: dropped digits (`402011000521` → `4020110521`), lost leading
zeros (`000512348899` → `95058000000`), and total loss (`1234500067` → nothing recovered;
one clip the TTS appears to skip entirely — Deepgram heard `आपका खाता number है।`).

**The single raw "success" is a scoring artifact, and this matters.** `9157114007` was
scored recovered because Sarvam's inverse text normalization reconstructed digits from a
spoken *quantity* — what was actually said was
`नौ अरब पंद्रह करोड़ इकहत्तर लाख चौदह हज़ार सात` ("nine billion fifteen crore…"). A borrower
hearing that cannot verify their account number. **By ASR the raw arm scores 1/5; by human
comprehension it is plausibly 0/5.** This is precisely the ASR-as-proxy failure that
SP-MCQA (arXiv:2510.26190) documents, arriving in our own pilot data on day 1 — and it is
the strongest available argument for why the human panel in §7 is not decoration.

**Changes to the test, effective now:**
1. Primary claim is scoped to `account_identifier` and any digit-sequence value.
2. `rupee_amount`, `percentage_apr`, `tenure_months`, `emi_amount` remain in the corpus and
   are still measured and reported — as the **negative result** they are.
3. Leading-zero preservation is added as an explicit scored property.
4. The human panel is now load-bearing rather than confirmatory, for the reason above.

**Unchanged:** conditions, channel, scoring method, corpus size, the §8 limitations.

### 2026-09-05 — `lang`-omitted timestamps tested and rejected

Omitting `lang` does emit word timestamps on a Hindi voice with correct Devanagari tokens,
but the values are byte-identical across sample rates while audio duration is not, at a
non-constant ratio (1.223–1.489). They do not describe the delivered stream. The consent
mechanic therefore uses our own flush-segment boundaries and byte-count duration, as
designed. Evidence in `evals/results/battery/FINDINGS.md`.
