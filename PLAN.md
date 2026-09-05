# SAMJHA — Build Plan

**Deadline: 8 Sept 2026.** It is now 5 Sept, 21:00 IST. ~3 days, not 5. Schedule below is rebuilt for that.

---

## Context

`~/Desktop/Dataforge` is empty and not a git repo. Greenfield build for the DataForge × Pathway × Rime track,
from a detailed design doc the user supplied.

**Problem.** RBI mandates a Key Facts Statement (KFS) for retail loans, in a language the borrower understands,
with informed acknowledgement. In practice lenders email a PDF and collect an OTP. A borrower with low reading
fluency — sold a loan over a phone call, where there is no screen — acknowledges a document she cannot read.

**Build.** A voice line that reads the KFS aloud in Hindi clause by clause, tracks which comprehension-critical
values she actually *heard* before interrupting, requires spoken teach-back, and emits a signed consent record
made of audio. Remove speech and the product doesn't degrade — it ceases to exist.

**User decisions:** Pathway = bonus only. Full stack, no pre-emptive cuts. Keys not yet obtained.
**MVP channel = browser call imitating a phone line**; no live PSTN leg (documented as post-MVP below).

**Research verdict: the design doc is ~80% sound and 3 things in it are factually wrong.** All corrected below.
Two were confirmed by three independent agents each, against live `docs.rime.ai`.

---

## 🔴 The three corrections that change the build

### 1. Rime emits no word-level timestamps in Hindi — and fails silently

> "Timestamps are emitted only when `lang` is `en`/`eng` or `es`/`spa`, or when `lang` is omitted. Requests in
> any other language receive `chunk` and `done` events with **no `timestamps` event and no error** (this
> includes fr, de, ja, pt, ar, and hi)." — `docs.rime.ai/api-reference/coda/websockets-json`

LiveKit's `use_tts_aligned_transcript=True` is fed by that same event, so in Hindi it degrades to **sentence-level**.

**Fix — better than the original design, and mostly already built by LiveKit.** Two halves:

*Half 1 — restructure the text.* The normalizer already must enforce *one value per sentence* for
intelligibility. Do that, and sentence-level granularity **is** value-level granularity.
`key_value_spans` → **`key_value_segments`**.

*Half 2 — use LiveKit's real playout clock, not Rime's timestamps.* `PlaybackFinishedEvent` carries
**`playback_position`** — float seconds of audio *actually played out*, not generated — plus `interrupted: bool`.
That is a measured value, not an estimate. Combined with per-segment durations (exact, from the audio we
received), it resolves precisely which segments completed.

```
clause → normalizer → chunks, each carrying AT MOST ONE key value
       → each chunk = its own flush segment (segment=never + explicit flush)
       → PlaybackFinishedEvent.playback_position ÷ known per-segment durations
       → "did the segment carrying the number finish playing?" — exact
```

⚠️ **Do NOT use `synchronized_transcript` for this in Hindi.** It looks like the obvious API and it is a trap:
without real word timestamps the transcript synchronizer estimates position from an audio-energy speaking-rate
detector plus `tokenize.basic.hyphenate_word` — **Latin-script syllable counting applied to Devanagari**.
`playback_position` is real; `synchronized_transcript` in Hindi is a guess.

Demo line: *"We don't need the vendor's word timestamps — we restructured the text so we don't have to."*
Works in every language, removes a vendor dependency from the central claim.

### 2. `contextId` is not a fence, and there is no cancel primitive

> "`clear` does not cancel an in-flight synthesis and does not drop text already queued by an earlier flush."
> "Rime does not maintain multiple simultaneous context IDs."

Naive contextId-discard **mis-attributes audio**: send two texts before synthesis starts and "only the later
context ID will be reflected on the first audio chunk."

**Barge-in is therefore:** stop client playout immediately → send `{"operation":"clear"}` → **close and reopen
the socket** for a hard stop. Keep a **warm spare connection** so reconnect latency doesn't land on the user.
This is honest and still a good talking point.

### 3. `/textnorm` is not a Coda oracle — and Rime's docs argue against our thesis

`/textnorm` (host `optimize.rime.ai`, *different base URL*) previews the **Mist** grammar normalizer. Coda's
normalization is "handled by the model itself, not a normalizer stage." Golden tests against `/textnorm` would
measure the wrong pipeline. Its honest role: a *design aid*, and a demo slide showing Rime's normalizer is
English-first.

Worse: Rime's own docs open with **"Most applications shouldn't pre-normalize."** They do carve out an exception
for "regulatory disclosures, legal read-backs, or confirmation flows" — **cite that exact phrase** and scope the
claim narrowly: *Hindi + Coda + regulated read-back + 8 kHz*. The tonight A/B is now mandatory, not precautionary.

---

## The one hard claim

Goes verbatim into `RIME_EVIDENCE.md`:

> On a Hindi-language, 8 kHz μ-law telephone-grade channel, SAMJHA's **delivery layer** (Indic normalization +
> one-value-per-segment chunking) causes comprehension-critical financial values — ₹ amounts in Indian digit
> grouping, percentages, tenures, EMI amounts and account identifiers — to be recovered **exactly** at a
> materially higher rate than the same clause text sent to Rime unprocessed, with `modelId`, `speaker`, `lang`,
> `samplingRate`, `timeScaleFactor` and endpoint held constant.

Condition B is *the delivery layer*, not "the normalizer" — chunking is required for the consent mechanic
anyway, so measuring them together is honest. Say so explicitly.

**Note the confound to disclose:** raw-arm failures may come from TTS mis-reading digit runs, not from the 8 kHz
channel. Log the *intended reading* (digit-by-digit vs cardinal) per value so the two are separable.

**Published prior art supports this claim — cite it.** SP-MCQA (arXiv:2510.26190, 2025) finds that **ASR WER is
a poor proxy for whether a listener recovers key information**, and that SOTA TTS specifically fails at *text
normalization* — proposing key-information QA in place of WER. That is our thesis and our metric choice
(VER over WER), independently published. A second 2026 paper documents exactly our failure mode in Indic TTS:
digit strings read as Indian-scale quantities instead of digit-by-digit, **fixed by pre-normalization**.

⚠️ Counterintuitive and relevant to scorer choice: **near-perfect ASRs correlate *weakly* with human
judgement** (their near-zero WER compresses the signal); mid-tier ASRs correlate more strongly. Another reason
the human panel is not decoration.

---

## Tonight's test battery — before any product code

Six experiments, ~45 min, all at `modelId=coda / speaker=taru / lang=hi` held constant. **The whole plan
branches on these.**

| # | Test | Why it's a project-killer if assumed |
|---|---|---|
| 1 | **Devanagari vs romanized** input, same sentence | Docs say *nothing* about script. Everything downstream — normalizer output alphabet, teach-back matching — depends on it. |
| 2 | `lang` **omitted** + `speaker=taru` → do timestamps appear, is the Hindi still intelligible? | The only documented loophole. Either saves or kills the original design. Assume it fails. |
| 3 | `₹1,04,596` vs `104596` vs spelled-out Hindi words | The A/B gap itself. If it's small, pivot tonight (below). |
| 4 | Bare 5+ digit run vs comma-separated | Docs: bare runs read **digit-by-digit**, comma-separated read as **quantities**. This *inverts* by formatting — load-bearing in both directions (good for account numbers, bad for rupees). |
| 5 | `spell()` passthrough on Coda | `models.md` says supported; `spell.md`, `text-normalization.md`, `prompting.md` all say not. Docs contradict themselves. |
| 6 | `/textnorm` at `lang=hi` vs `en` on rupee strings | Establishes what Rime's own normalizer does — the true baseline to beat. |

**Gate at midnight:** if test 3 shows no meaningful gap, pivot the headline *tonight*, same product, same demo:
1. `account_identifier` + forced digit-by-digit (test 4 says formatting alone drives this — usually the biggest gap).
2. Promote **heard-through consent gating** to the primary claim; demote delivery to secondary.

Discovering this on day 3 is the single most likely way this build fails.

---

## Rime configuration — verified 2026-09-05

```
wss://users-ws.rime.ai/ws3?speaker=taru&modelId=coda&lang=hi&audioFormat=mulaw&samplingRate=8000&segment=never
Authorization: Bearer $RIME_API_KEY
```

| Field | Value | Why |
|---|---|---|
| `modelId` | **`coda`, pinned explicitly** | **Defaults to `mistv3`**, which has zero Hindi. An unrecognized value is *also* served by mistv3. One typo silently destroys the model-held-constant premise. |
| `speaker` | `taru` (m, "balanced, professional sounding native Hindi speaker"); `nadi` (f) as the alternate | Coda has **exactly 2** Hindi voices out of 253. |
| ⚠️ | **`anaya` / `anil` / `arya` are traps** | Retired **Arcana** voices (retired 2026-08-15) still listed in `all-v2.json`. |
| ⚠️ | `all-v2.json` has a **data bug** | The lang code is injected as a phantom speaker: `coda.hin = ["hin","nadi","taru"]`. Filter it or you'll build a voice picker with a fake voice in it. |
| `lang` | `hi` | Unsupported voice/lang pairings "may not return an error". |
| Speed | **`timeScaleFactor` = 1.0, held constant. Never send `speedAlpha`.** | Three-way trap: Mist v2 `<1.0` faster; Coda `speedAlpha` `>1.0` faster; Coda `timeScaleFactor` `<1.0` faster. `timeScaleFactor` is silently clamped to 0.4–2.5. |
| Chunking | `segment=never` + explicit `{"operation":"flush"}` | We control utterance boundaries. This is what makes segment-level playout accounting exact. |
| Limits | **1000 chars/request**, HTTP 400 beyond | Hold chunking constant across both arms — chunk boundaries affect prosody, therefore comprehension. |
| Region | US West (default) / US East. **No India region.** | From India expect 250–350 ms to first audio. Report network latency separately from model latency. Rime's published 96 ms P50 is a self-hosted H100 number, not cloud. |
| HTTP path | Set the `Accept` header correctly | A missing/unrecognized Accept returns **HTTP 200 with JSON containing base64 `audioContent`**, not audio bytes. A batch script checking only status codes fills `results/` with non-audio files that "succeeded". |
| Browser | Cannot call `/ws3` directly | Browsers can't set `Authorization` on a WebSocket. Proxy through the backend — the key never reaches the frontend. |

**Not available on Coda, at all:** `phonemizeBetweenBrackets` (accepted and *silently ignored*),
`pauseBetweenBrackets`, `inlineSpeedAlpha`, SSML, `noTextNormalization` (Mist-only, so you cannot build a clean
"normalizer off" control arm on Coda — the contrast must live entirely in our text layer). Hindi is Coda-only,
so there is no fallback model that has these knobs. **A text pre-normalization layer is the only lever.**
Cite the Coda parameter list + the language matrix as the two-line proof.

`make preflight` pulls the live catalog and hard-fails if `(modelId, speaker, lang)` is absent. Runs in CI.

---

## Stack — verified against PyPI, 2026-09-05

**Python 3.13.15 via `uv`.** (3.14 was suspected as a blocker; it isn't — every package has cp314 arm64 wheels.
3.13 is chosen because for *this* dep set it's a strict superset, and this is not the week to debug wheels.)

```bash
uv python install 3.13.15 && uv init samjha && cd samjha && uv python pin 3.13.15
uv add "livekit-agents[deepgram,silero,turn-detector,openai,rime]==1.8.0" \
       "livekit-plugins-sarvam==1.8.0" \
       "onnxruntime==1.29.0" "numpy==2.5.2" "scipy==1.18.1" \
       "soundfile==0.14.0" \
       "pdfplumber==0.11.10" "pypdf==6.17.0" "pydantic==2.13.5" \
       "fastapi==0.141.1" "uvicorn==0.52.4" "indic-numtowords==1.1.0" python-dotenv
```

⚠️ **`pip install num2words` has NO Hindi.** `lang_HI.py` was merged to master 2025-01-09 — *three weeks after*
0.5.14 shipped, and 0.5.14 is still latest on PyPI. `num2words(125000, lang='hi')` throws. Hours lost if
assumed. (`lang='en_IN'` *is* released and does lakh/crore — useful as the code-switch arm.)

⚠️ The package is **`indic-numtowords`** (AI4Bharat, MIT) — *not* `indic-num2words` (raj-sutariya), whose own
README output concatenates unit to number without a space (`पैंतीसकरोड़`), which changes how Rime phonates the
boundary. Wrap `indic-numtowords`: it does `.isdigit()` and **throws on decimals, negatives and currency
symbols**, so split integer/fraction/sign yourself. It uniquely gives `variations=True` (colloquial forms) and
`split=True` (digit-by-digit) — both of which we need.

**No `audioop-lts` needed** — the channel is done in ffmpeg (below), which removes an unmaintained C extension
from the critical path entirely.

| Excluded | Why |
|---|---|
| `pydub` | **Crashes on import** on 3.13+. Dead since 2021; its ImportError fallback is a Python-2 implicit relative import. Use ffmpeg 8.1 (already installed) + `soundfile`. |
| `nemo-text-processing` | Depends on `pynini` → OpenFst. **No reliable macOS-ARM wheels** — conda-forge/Linux-container only. Don't discover this at 2 am. Lift its Hindi test data instead (below). |
| `webrtcvad` | No wheels; the fork stops at cp313. Silero is already in the pin set and is what the framework expects. |
| `num2words` (for `hi`) | Two independent reasons: it drags `docopt 0.6.2` (sdist-only, 2014, classifiers stop at 3.3 — likeliest cause of a failed first `uv sync`), **and its Hindi doesn't exist on PyPI at all**. Keep it only if we want the `en_IN` code-switch arm. |
| `torch` | `turn-detector` runs on onnxruntime + transformers-tokenizer only. Saves ~2 GB on day 0. |
| `librosa` | Pulls numba/llvmlite/sklearn for what `scipy.signal` already does. |

⚠️ **`audioop` was removed from the stdlib in 3.13 (PEP 594)** — confirmed missing on this machine. Every
Stack Overflow μ-law snippet uses it. `audioop-lts` would restore it, but we don't need it: **ffmpeg does the
whole G.711 channel**, which keeps an unmaintained C extension off the critical path of a demo.

⚠️ If scaffolding from LiveKit's `agent-starter-python`, **delete its `constraint-dependencies = ["yarl<1.24"]`** —
fixed upstream by yarl 1.24.5 and it silently pins you backwards.

⚠️ `livekit-plugins-sarvam` and `-deepgram` both shipped **1.8.0 today**. Pin exact; a mid-hackathon bump on a
plugin released hours ago is a real risk. `livekit-plugins-rime`'s PyPI classifiers (3.11/3.12 only) are stale
metadata — `Requires-Python >=3.10`, pure-Python wheel.

---

## LiveKit Agents 1.8.0 — four defaults that will silently break this product

The whole plugin ecosystem is lockstep-versioned at **1.8.0, published 2026-09-05** (today). The entrypoint
shape changed: `server = AgentServer()` + `@server.rtc_session()` + `cli.run_app(server)`. `WorkerOptions` still
exports for back-compat, but every tutorial you find is showing the old shape.

**All flat barge-in kwargs are deprecated** (`allow_interruptions`, `min_interruption_duration`,
`turn_detection`, `preemptive_generation`, …) → one `turn_handling=TurnHandlingOptions(...)` object.

| Default | What it does to a consent product |
|---|---|
| `resume_false_interruption=True`, `false_interruption_timeout=2.0` | The agent **resumes reading a clause** after a "false" interruption — playing audio our bookkeeping already closed out. **Disable it.** Our rule is re-read from the start, always. |
| `backchannel_boundary=(1.0, 1.0)` | Overlapping speech within 1 s of a turn's start or end is **suppressed as a backchannel**. A borrower saying *"हाँ"* right after a rupee amount **may not register as an interruption at all** — a direct hole in Stress B. Set to `(0, 0)` and classify explicitly. |
| `rime.TTS(use_websocket=False)` | The default. Silently sets `streaming=False` **and** `aligned_transcript=False`. No error, no warning. The single easiest way to build the whole product on sand. **Always `use_websocket=True`.** |
| Interrupt before any frame plays | `forwarded_text=""` and the assistant message is **dropped entirely** — no chat item at all. The heard-ledger must treat *no event* as **heard nothing**, never as heard everything. |

**Don't write a TTS plugin.** Override `Agent.tts_node()` instead, and inject the normalizer via
`AgentSession(tts_text_transforms=[...])` with the built-in `transcription.text_transforms.replace({...})`
helper — a supported hook for exactly this. Only subclass `tts.TTS` if we must emit our own frames. This
removes most of the doc's day-3 custom-adapter risk.

Interruption mode can be `'adaptive'` (ML) or `'vad'`; 1.8.0 also ships `AdaptiveInterruptionDetector` in the
new first-party `inference` module.

---

## STT — Sarvam Saaras

⚠️ **Saarika is gone.** `saarika:v2` / `v2.5` will 400 or silently migrate. Enum is now `saaras:v3` / `saaras:v4`.

**Live:** `sarvam.STTRealtime(language="hi-IN", mode="codemix", endpointing="vad")` on `saaras:v4-realtime`.
First-party LiveKit plugin, native 8 kHz, leads Hindi telephony WER (5.0 vs Deepgram's 13.0). `codemix` keeps
English loan words in Latin script — which makes teach-back matching against an English-loanword-heavy KFS far
easier than forcing everything into Devanagari.

**Don't invent a provider interface.** `livekit.agents.stt.STT` already *is* one. One factory function behind
`STT_PROVIDER=sarvam|deepgram`, ~15 lines. Fallback: `deepgram.STT(model="nova-3", language="hi")` — **explicitly
`hi`, never `multi`** (documented, staff-acknowledged Hindi→Spanish misdetection on Hinglish calls).

⚠️ `sarvam.STTRealtime` is **Python-only**; the JS SDK silently gives you `saaras:v3` and no realtime class.

**Derive "heard up to segment N" from playout position + VAD speech-start — never from the STT transcript.**
That decouples the central measurement from ASR error entirely.

---

## The eval harness

`evals/ACCEPTANCE.md` committed **first**, before the agent exists. `git init` tonight — the timestamp is the
evidence that the test preceded the result. This is a scoring artifact.

**Corpus.** 10 synthetic KFS docs → 120 utterances, 6 categories (`rupee_amount`, `percentage_apr`,
`tenure_months`, `emi_amount`, `account_identifier`, `date_deadline`), each in a Hindi carrier sentence.
Ground truth as **typed values** — `{"type":"rupee_amount","value":125000}` — so scoring is `==` on a number.

**Scoring — the highest-leverage decision in the harness.** Sarvam's `mode` parameter deterministically controls
numeral format. So run **every clip through the same audio twice**: `mode="transcribe"` (returns `9840950950`)
*and* `mode="verbatim"` (returns spoken words). **Score as recovered if either pass matches after normalization.**
This removes the ITN-format confound instead of fighting it. Add Deepgram `nova-3 language="hi"` as a second
independent scorer and **report the agreement between them**.

⚠️ Three scoring traps, each of which silently fakes the result:
- **Provider numeral format is not comparable.** Sarvam `transcribe` → digits; Deepgram Hindi → spelled-out words
  (`numerals` is unsupported for Hindi). A digit regex scores Deepgram ~0% on every value.
- **NFKC does not fold Devanagari digits** (१२३ → 123). Needs an explicit U+0966–U+096F map.
- **Sarvam `verbatim` mixes scripts inside one number**: `नौ आठ चार zero नौ पांच zero` — Latin "zero" inside
  Devanagari. A Devanagari-only word→digit map drops every zero in an account number.
- Plain WER marks correct answers wrong: `5 lakh` / `five hundred thousand` / `500000` are one value, three
  strings. Score parsed numerics with an Indic multiplier table (लाख/करोड़/हज़ार).

**Channel — two ffmpeg lines, verified working on this machine's ffmpeg 8.1:**

```bash
ffmpeg -y -i clean.wav -af "aresample=8000,highpass=f=300:p=2,lowpass=f=3400:p=2" \
       -c:a pcm_mulaw -ar 8000 -ac 1 tel_ulaw.wav
ffmpeg -y -i tel_ulaw.wav -c:a pcm_s16le -ar 8000 tel_pcm.wav
```

⚠️ Four traps, each verified locally:
- **`resampler=soxr` hard-fails** on the Homebrew macOS build (libsoxr not compiled in). Every blog recipe
  recommends it. Use the default swresample engine.
- **ffmpeg's `bandpass` filter is the wrong shape** — a Q-based peaking BPF, not a 300–3400 band. Cascade
  `highpass:p=2` + `lowpass:p=2`.
- **`amix` normalizes by default**, dividing each input by N and silently destroying your target SNR by 6 dB.
  `normalize=0` is mandatory.
- **`scipy.io.wavfile` cannot read μ-law** (WAVE_FORMAT_MULAW, fmt tag 0x0007). Decode back to pcm_s16le with
  ffmpeg before any Python analysis, or use `soundfile`.

**Noise:** DEMAND (Zenodo `10.5281/zenodo.1227121`, CC BY 4.0, 7.4 GB) has cafeteria / traffic / square / bus /
metro — exactly a rural-India phone call. MUSAN (openslr.org/17, CC BY 4.0, 11 GB) is the fallback. **Freeze one
noise file and 2–3 SNR points (20/10/5 dB) and hold them constant.** Note the rigorous SNR reference is
**ITU-T P.56 active speech level**, not whole-file RMS — a clause with long pauses measures as quieter than it
is, so RMS-based mixing under-adds noise. Disclose which we used.

⚠️ **Order of operations is not commutative.** Band-limit and resample *before* μ-law. Add noise *before* the
codec to simulate a noisy handset, *after* the band-pass but before μ-law to simulate line noise. **Pick one,
document it, hold it constant across both arms.**

One command: `make eval`.

**Metrics.** Primary: **Value Error Rate (VER)**. Secondary: per-category VER, WER, TTFB **warm and cold
reported separately**, barge-in stop latency P50/P95.

**Human panel.** 3 native Hindi speakers, blind, 30 randomly ordered clips. Labelled exploratory. **Report the
disagreement with ASR — reporting it is what scores.**

**Limitations, written by us first.** ASR error ≠ human misunderstanding. n=120, 3 listeners, exploratory. One
accent register, one noise profile. Only 2 Hindi voices exist, so voice-level variance cannot be averaged over —
hold speaker constant and say so. "Heard" means *played out of the speaker*, ± jitter buffer — we measure
playout, not cognition. **Not a TTS benchmark; we do not compare Rime to other providers, and we say so.**

---

## Hindi verbalization — don't hand-write it, but don't trust the obvious library either

AI4Bharat's own shipped Indic-TTS pipeline uses **three** libraries together; mirror that rather than inventing
a verbalizer.

| Layer | Use |
|---|---|
| `indic-numtowords` | Integer verbalizer. Hindi lakh/crore, `variations=True`, `split=True`. Wrap it — throws on non-integers. Tops out at crore, then silently degrades to digit-by-digit. |
| **NeMo Hindi test data** | **Our gold reference.** `tests/nemo_text_processing/hi/data_text_normalization/test_cases_{money,cardinal,decimal,telephone}.txt` are plain `input~expected` pairs. **Copy them into the repo as the regression suite** — no pynini install needed. This replaces the `/textnorm` golden-test hole with a real gold source. |
| `num2words` `lang='en_IN'` | The English-Indian code-switch arm (released, does lakh/crore). |

Gold forms to build against: `₹1254000` → *बारह लाख चौवन हज़ार रुपए* · `3,24,50,000` → *तीन करोड़ चौबीस लाख पचास हज़ार*
(comma-grouped input parses). Indian grouping is 2-2-3 from the right; हज़ार 10³, लाख 10⁵, करोड़ 10⁷, अरब 10⁹.

**Five rules where every library disagrees or has a hole — write and unit-test these on day 1:**

1. ⚠️ **Percentages: no library covers them.** NeMo's Hindi grammar has **no `%` / प्रतिशत at all** — verified
   absent from `measure.py`, `graph_utils.py`, `unit.tsv` and the test cases. **APR is the single most
   comprehension-critical value in a KFS and it is the one class nothing handles.** Hand-write it.
2. ⚠️ **Money decimals are not दशमलव.** `₹123.57` → *एक सौ तेईस रुपए सत्तावन पैसे* (paise), but `99.99` →
   *निन्यानबे दशमलव नौ नौ*. Two rules for the same `.`, switched by the currency symbol. A single naive decimal
   rule produces a wrong-but-plausible reading a Hindi speaker will catch instantly.
3. ⚠️ **Account numbers: do not use NeMo's telephone grammar.** It inserts a **spurious leading शून्य** on
   10-digit numbers (11 spoken digits for 10 input digits) — a systematic corruption of exact-recovery scoring.
   Roll digit-by-digit with शून्य ourselves.
4. **Colloquial collapse is the natural form**, encoded as first-class constants in NeMo: डेढ़ (1.5), ढाई (2.5),
   सव्वा (1.25), साढ़े (.5), पौने (.75). *साढ़े तीन लाख* is what a borrower actually says. Anchor-and-confirm can
   pair the colloquial with the literal.
5. **Code-switching is real, not a nicety.** AI4Bharat's production `text.py` substitutes the **English word
   "point"** for `.`, not दशमलव. If our gold transcript says दशमलव and the borrower's natural form is "point",
   the scorer must accept both.

⚠️ `indic-nlp-library` is **not** this. Its "Text Normalization" is Unicode/ZWJ canonicalization — no number
verbalization at all. Using it as "the Indic normalization layer" delivers none of the claimed benefit.

---

## Consent state machine

```
UNHEARD ──deliver──▶ HEARD ──teach-back pass──▶ UNDERSTOOD
   │                   └──fail──▶ TEACH_BACK_FAIL ──re-deliver──▶ HEARD
   └──barge-in before key_value_segment completes──▶ PARTIALLY_HEARD ──re-deliver from START──▶ HEARD
```

Re-delivery **always restarts the clause from the beginning**. `UNDERSTOOD` is the only state permitting consent.
A refused consent is written into the record — it's a feature, not an error.

### Stress cases — these *are* the demo

**A — barge-in mid-number.** Audio stops within target latency (measured, P50/P95); socket closed+reopened, no
stale chunk replays; LLM/tool calls *cancelled*, not ignored; clause flips to `PARTIALLY_HEARD` on screen; the
answer goes through the normalizer; clause re-read **from the start**; consent stays blocked.

**B — rushed consent.** *"हाँ हाँ ठीक है, बस करो"* two seconds in. System refuses, flags for human callback,
writes the refusal to the record.

**C — the off-switch.** Same clause, one config flag, both clips back to back. 15 seconds of audio carrying the
entire 20% evidence score.

---

## The channel — MVP is a simulated phone line in the browser

**MVP decision (user's call): no live PSTN leg.** The demo is a browser call presented as a phone call. This is
the right MVP trade — a broken SIP leg on stage is fatal, and the phone framing is a *product* claim, not a
transport claim.

**Make the imitation real, not cosmetic.** Rime emits 8 kHz μ-law directly, so request the **same audio format
for the web path as for the eval** (`audioFormat=mulaw&samplingRate=8000`) and apply the same 300–3400 Hz
band-pass. Then:
- the demo *sounds* like a phone call, so "there is no screen" lands audibly;
- **the demo condition and the eval condition are the same channel** — the A/B numbers describe the audio the
  judge is actually hearing. That is a stronger evidence story than a real phone line with an unmeasured codec.
- UI is styled as a call: dialpad → ringing → in-call, with the clause-state panel beside it.

⚠️ **Say this out loud, in the README and the demo:** *"We measure and demo over a simulated telephone channel.
We have not validated on a live PSTN leg."* Disclosed limitations score; unsupported claims are punished.

**Post-MVP path, documented and costed (do not build now):** LiveKit Cloud Build tier gives **1 free US number,
inbound-only, no SIP trunk config** — "zero to ringing in 60 seconds" — plus 50 inbound minutes and 1,000
agent-session minutes. ⚠️ 5 concurrent agent sessions; serialize callers or budget $50/mo for Ship.
**Cut Twilio and +91 numbers entirely** — Indian DIDs need KYC and multi-day regulatory review on Twilio, Plivo
*and* Exotel, and Twilio trial inbound only accepts calls from numbers verified in that same account (max 5).

**Total cash cost of this project: $0.** Rime free tier is self-serve, no card, includes Coda *and* `/ws3`
(~800k chars). Sarvam and Deepgram both self-serve.

---

## Architecture & layout

```
   browser (styled as a phone) ─▶┌────────────────────────────────┐
   8 kHz μ-law + band-pass       │    LiveKit Agents (Python)     │
   [PSTN — post-MVP]             │                                │
                                 │  Silero VAD ─ turn ─ Sarvam ─  │
                                 │  LLM ─ Rime /ws3 TTS node      │
                                 └───┬────────────────────┬───────┘
                     ┌───────────────▼──┐    ┌────────────▼──────────────┐
                     │  Consent FSM     │◀───│  Delivery layer           │
                     │  + rushed gate   │    │  normalizer → chunker →   │
                     └────────┬─────────┘    │  /ws3 (byte-exact playout)│
                              │              └───────────────────────────┘
                     ┌────────▼─────────┐         │ appends JSONL
                     │  Consent record  │         ▼
                     │  (hashed)        │    events/{call_id}.jsonl ──▶ [Pathway, stretch]
                     └──────────────────┘
```

```
samjha/
├── README.md  RIME_EVIDENCE.md  .env.example  Makefile
├── delivery/          # ← the hard problem
│   ├── normalizer/    indic_numbers.py percentages.py tenure.py identifiers.py
│   ├── chunker.py     # one key value per segment ← also the timestamp fix
│   ├── rime_ws3.py    # flush control, per-segment durations, warm spare socket
│   ├── tts_node.py    # Agent.tts_node() override — NOT a custom tts.TTS plugin
│   └── config_guard.py
├── agent/             main.py session.py consent_fsm.py teachback.py rushed_consent.py
├── kfs/               schema.py extract.py clauses.py
├── api/  web/  fixtures/synthetic/
├── evals/             ACCEPTANCE.md corpus/ run_eval.py channel.py asr_score.py latency.py
│                      human_panel/ results/{clips,item_level.csv,summary.md}
└── tests/test_normalizer.py
```

**KFS schema:** circular **RBI/2024-25/18**, Annex A. ⚠️ Part 1 is **not 10 flat fields** — rows 3, 5, 7, 8, 10
are nested sub-tables; row 1 carries two fields under one serial number. Model it nested. **APR = monthly IRR ×
12 (nominal, *not* compounded), computed on the amount NET of all fees.** RBI's own Annex C illustration doesn't
foot — don't "fix" it to match. Part 2 items 5–6 are permissive ("may be furnished"), not mandatory.
The regulatory hook to quote: the KFS "shall be written in a language understood by the borrowers."

---

## Schedule — 3 days

**Tonight, Sat 5 (21:00–24:00) — the gate.**
`git init`. Commit `evals/ACCEPTANCE.md` **first**. Sign up Rime (~5 min). `uv` setup + `uv run python -c "import
livekit.agents, onnxruntime, soundfile; from indic_numtowords import num2words"`. `config_guard.py` against the live catalog. **Run the six-test
battery.** Record the failure clip — that's the demo's "before". **Decide at midnight whether the claim holds.**

**Day 1, Sun 6 — delivery layer + eval.**
Indic normalizer (`indic-numtowords` + the five hand-written rules, regression-tested against the lifted NeMo
Hindi gold pairs) + chunker. 10 synthetic KFS docs, 120-item typed corpus. `channel.py`,
`asr_score.py` (dual-mode Sarvam + Deepgram cross-check), `run_eval.py`. **First A/B number by evening.**
If the gap is small, iterate here — this is the whole claim. Nothing else gets built until this number exists.

**Day 2, Mon 7 — agent.**
LiveKit session (`AgentServer` shape), Sarvam STT, clause loop, consent FSM, teach-back grading, rushed-consent
gate. `PlaybackFinishedEvent.playback_position` wired to `key_value_segments`. **Set the four dangerous defaults
first** — `resume_false_interruption=False`, `backchannel_boundary=(0,0)`, `use_websocket=True`, no-event-means-
heard-nothing — before building on top of them. Barge-in: stop → clear → close/reopen, warm spare. Measure stop
latency, put it on screen. Web panels — **large, coloured, slow enough to read on a compressed video**, or Stress
A looks like nothing happened. *Checkpoint 20:00: if the UI isn't up, drop Next.js for one static HTML page fed
by the agent's websocket — 1 hour instead of 4.*

**Day 3, Tue 8 — evidence and demo.**
Wire the 8 kHz μ-law + band-pass channel into the *live* web path so demo condition == eval condition; style the
UI as a call. Rehearse Stress A and B until boring. Human panel: 3 listeners, 30 clips. Final `make eval`, commit clips + `item_level.csv` + `summary.md`. Write `RIME_EVIDENCE.md` and `README.md`.
Secret sweep: `git log -p | grep -iE 'rime|api[_-]?key|sk-'` — rotate anything that ever touched a commit.
**Record the demo, 3 takes minimum. Submit with hours of buffer, not minutes.**

**Pathway — stretch only, gate at Day 2 20:00.** If the agent is done: 4–8 h, separate process (`pw.run()` blocks
forever, it cannot share the agent's asyncio loop). Agent appends JSONL → `pw.io.fs.read(mode="streaming")` →
**`asof_join(direction=BACKWARD)`** of the spoken-value stream against the consent stream = "did she hear the APR
before consenting?", written as one operator. Bare `pip install pathway` only — **never `[all]` or
`[xpack-llm-docs]`** (paddleocr has no py3.14 wheel and a Feb-2025 transformers pin). Demo it by replaying a
recorded call's JSONL into the watched folder while the dashboard updates live. Skip the RAG index — it's the
most obviously sponsor-shaped thing you could bolt on. At 3 days, expect to cut this; that's fine, it's bonus.

---

## Verification

1. `make preflight` — live catalog check; hard-fails on a stale `(modelId, speaker, lang)` triple. Must pass in CI.
2. `uv run pytest tests/test_normalizer.py` — regression against the **lifted NeMo Hindi gold pairs**
   (`input~expected`), plus our own cases for the five uncovered rules. Not `/textnorm`.
3. `make eval` — full A/B, one command, reproduces every number in the README from committed artifacts.
4. `make demo-fixtures` then a scripted call: verify a barge-in mid-number leaves the clause `PARTIALLY_HEARD`
   and that consent is refused; verify Stress B refuses and writes the refusal to the record.
5. Browser call over the simulated 8 kHz μ-law channel, end to end. Active provider observable on screen
   throughout. Confirm the README states the PSTN leg was not validated.
6. Eligibility sweep: Rime is the primary spoken output in the judged flow; `.env.example` placeholders only; no
   credential in git history, screenshots or the recording; all data synthetic and stated; cached vs uncached
   labelled separately; every performance number backed by a committed artifact.

---

## Risk register

**The gap is small.** Test tonight, hour 2. Pivots pre-staged above. Discovering this on day 3 kills the build.
**Scope creep.** Anything not clause delivery, teach-back, or consent gating costs points. Auth: cut.
Multi-language: cut. Document upload UI: ship fixtures.
**The state machine isn't legible on the recording.** Design the panel for a compressed video, not a desk.
**Honesty gap.** No claimed PSTN result we didn't measure; no claimed human comprehension where we measured ASR.
Rime's docs contradict themselves in three places — **do not cite them as internally consistent** in the writeup.
Third-party wrapper docs (Pipecat, VideoSDK, older LiveKit pages) still describe `reduce_latency`, `temperature`,
`top_p` that appear nowhere in Rime's first-party docs. Don't trust wrapper docs for the parameter surface.
