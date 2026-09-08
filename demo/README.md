# demo/

## video/samjha_demo.mp4 — 179s, the whole flow

```bash
open video/samjha_demo.mp4
```

Rebuild it with `make serve` in one terminal and `make video` in another. Playwright drives
Chromium through the three surfaces while a real Rime-backed run paces the events
underneath.

| Beat | Time | What is on screen |
|---|---|---|
| Intake | 0–12s | `demo_kfs.pdf` uploaded, every Annex-A field read, then the **Hindi that will be spoken** with each value's spoken form |
| The call | 12–162s | The borrower page: loan in 44px digits, one green tap, then clause after clause — arcs turning green, the heard-through bar advancing, each number grey until its segment finishes |
| The barge-in | ~30s | `identity` cut 0.7s before the account number's segment ends. Its arc goes **red**, the number is never credited |
| Panel | 162–172s | The judge's evidence panel over the same call |
| Record | 172–179s | **REFUSED**, flagged for human callback, the sha256, and `identity` at **96.6% heard** with `000248710401483` struck through |

### Two limits, stated because they are not visible in the file

- **Chromium records the page, not the speakers.** So the audio is the *same run's* Rime
  output — synthesized to WAV by `scripts/e2e.py`, at the same durations, for the same
  document — muxed in afterwards. `--realtime` paces each clause by its own measured
  duration, which is what makes the two line up. It is not a re-recording and not a
  different take, but it is muxed rather than captured.
- **No agent process spoke over WebRTC in this recording.** The borrower page really joins
  a real LiveKit room with a real token, but the clause states come from the event stream
  the driver writes, and the agent dispatch is stubbed so only one thing drives the call.
  `make serve` + `make agent` + a browser is a fully live call; it just cannot be captured
  with audio from here.

The record header therefore reads `MODE: LIVE` — correct in the sense the field means (a
real document, not `api/demo.py`'s fixture replay), but no LiveKit agent read it. The
call's own event log carries that disclosure as a `note`, and it shows in the ticker
during the panel beat.

Everything else is the real product: the real pages, a real parse of a real document, real
Rime Coda audio, the real state machine, the real refusal.

---

## Audio artifacts, and recording the screen yourself

Everything in `audio/`, `full_call.*`, `bargein_cut.*` and `evidence_*.*` is **real Rime
Coda output** (`coda`/`taru`/`hi`, mu-law 8 kHz over `/ws3`), captured from a real run
against `demo/demo_kfs.pdf`. Nothing here is a mock-up.

Regenerate any of it with `make e2e ARGS="--doc demo/demo_kfs.pdf --drive --save demo/audio"`.

| File | Length | What it is |
|---|---|---|
| `full_call.wav` / `.m4a` | 159.3s | Every clause of the loan, in read order |
| `audio/<clause>.wav` | — | The ten clauses individually |
| `bargein_cut.wav` / `.m4a` | 20.2s | The `identity` clause, cut where the barge-in cuts it |
| `evidence_ab.wav` / `.m4a` | 16.4s | **The off-switch.** Same account number, raw then through the delivery layer |
| `evidence_a_raw.wav` | 8.0s | Arm A: `…संख्या 000512348899 है।` sent unprocessed |
| `evidence_b_delivery.wav` | 7.2s | Arm B: the same value, digit-by-digit |

## The two clips that carry the argument

**`evidence_ab.wav`** is the one to lead with. It is one corpus item
(`account_identifier_01`, ground truth `000512348899`, intended reading digit-by-digit)
spoken twice: unprocessed, a beat of silence, then through the delivery layer. This is the
category where the measured Value Error Rate went 100% → 0% (Deepgram) and 75% → 0%
(Sarvam). Sixteen seconds, no narration needed.

**`bargein_cut.wav`** is the consent mechanic. The `identity` clause runs 20.9s and the
account number lives in its third and final flush segment. The clip stops at **20.2s** —
0.7s before that segment ends, so **96.7% of the clause played and the number still does
not count as heard**. That is the whole product: the borrower heard almost all of it, and
"almost" is not consent. Play it, then show the panel marking the clause `PARTIALLY_HEARD`
with the value struck through.

## Recording the screen

There is no screen recording in here, and no automated one is possible from this repo —
there is no browser automation installed, so nothing can click through `/intake` and the
borrower page but a person.

macOS, no extra tools:

```bash
screencapture -v ~/Desktop/samjha.mov     # ctrl-C to stop
```

Or **⌘⇧5** for a region, or QuickTime → File → New Screen Recording (which can also
capture system audio, so Rime's voice lands in the file — `screencapture -v` records video
only).

### Shot list

Two terminals up first (`make serve`, `make agent` — wait for `registered worker`), and
`/intake` already open with `demo/demo_kfs.pdf` picked but **not** uploaded.

| # | Beat | Show |
|---|---|---|
| 1 | ~15s | Click **Read the document**. Every Annex-A field turning up `parsed`, then scroll to section 3 — *the Hindi that will actually be spoken*, with each value's spoken form |
| 2 | ~10s | Upload the messy variant instead (`make dummy` below): the drifted labels quoted verbatim, `Total charges (Rs)` refused as unrecognised, the cooling-off row red, the button disabled |
| 3 | ~5s | Type `3`, **Save these values** → the field goes amber `supplied by hand` |
| 4 | ~5s | **Create the call** → **Open the borrower page →** |
| 5 | ~20s | The borrower page: loan amount in 44px digits, one green button. Tap it. Rime starts reading in Hindi |
| 6 | ~15s | **Interrupt mid-number.** Cut to the panel: `PARTIALLY_HEARD`, the account number struck through, consent blocked |
| 7 | ~10s | The record at `/record/{id}`: `synthetic_data`, `kfs_provenance.sha256` matching the upload, the refusal as a row, `fields_corrected` naming the human |
| 8 | ~15s | `evidence_ab.wav`, full screen, nothing else on it |

For the messy document in beat 2:

```bash
PYTHONPATH=. uv run python scripts/make_dummy_kfs.py     # /tmp/dummy_kfs.docx
```

### Two things to say out loud, because they score

- *"We measure and demo over a simulated telephone channel. We have not validated a live
  PSTN leg."*
- The Hindi in the clause text and in the borrower page's prompts **has not been reviewed
  by a native speaker** (`TALK_SCRIPT.md:118`). Listen to `full_call.wav` before you
  record; if a sentence is wrong, that is a known open item, not a surprise.

`demo_kfs.pdf` is a generated specimen — no real borrower data.
