# DEMO — the runbook

What you **do**. [`PRESENTATION.md`](PRESENTATION.md) is what you **say** — the 9-minute
pitch script, the timings and the Q&A prep. Keep this one open on a second screen.

Everything below is verified against a real run on 2026-09-19: real Rime Coda audio, real
FSM, real sealed record. Durations are measured, not estimated.

---

## 0 · Commands

Three terminals. Your `.env` already has `SAMJHA_SKIP_INTRO=1`.

```bash
make serve          # terminal 1 — leave running
make agent          # terminal 2 — leave running, give it ~10s
make stage          # terminal 3 — must print GO
```

Then, on a second screen or printed:

```bash
make answers        # YOUR Hindi lines for demo/demo_kfs.pdf
```

### `make stage` must say GO

Two warnings are **expected and correct**:

- `SAMJHA_SKIP_INTRO  ON` — you want this. It is why the call opens on the account number.
- `tunnel  not given` — you are not using one. See below.

Anything red, fix it before you go on. It names the command.

### No tunnel. Run on `http://localhost:8000`

`getUserMedia` needs a secure context, and **`http://localhost` is one** — browsers exempt
it by spec, and `web/call.html` gates on `window.isSecureContext` and nothing stricter. A
tunnel is only needed to open the borrower page on a *separate phone*: a second device, a
second network path, and a second thing to fail in front of judges. The projector shows
your laptop either way.

---

## 1 · Green room, T−20

Do one complete call for real. This is your rehearsal **and** your stage artifact.

1. `http://localhost:8000/` → drop `demo/demo_kfs.pdf` on it → **Read the document**
2. **Create the call** → **Open the borrower page**
3. Take the call end to end: interrupt clause 1, answer 2–10 from the card, say हाँ
4. Confirm you get **CONSENT REFUSED**
5. **Leave that tab open.** It is tab 4 on stage.

        green-room call_id: ____________________

Its identity clause will read around **75–93% heard**, not the 96.6% on slide 2. Different
call, different clause length. Do not chase the number.

---

## 2 · Four tabs before you walk up

| Tab | URL | State |
|---|---|---|
| 1 | `/` | `demo/demo_kfs.pdf` uploaded and parsed. **Create NOT yet clicked.** |
| 2 | `/c/{id}` | opened after you click Create |
| 3 | `/panel` | the evidence panel |
| 4 | `/record/{green-room-id}` | the complete refusal |

Plus `demo/evidence_ab.wav` in a player, volume tested.

---

## 3 · The beats

Narration for each is in `PRESENTATION.md`. This is the clicking.

| At | Tab | Do |
|---|---|---|
| +0:00 | 1 | Scroll the parsed Annex-A fields, then scroll to section 3 — the Hindi that will be spoken |
| +0:25 | 1 | **Create the call** → **Open the borrower page** |
| +0:30 | 2 | **Tap the green button** |
| +0:32 | 2 | **~5 seconds of silence.** Talk through it — see below |
| +0:37 | 2 | Rime reads the account number, ~9.2s |
| +0:45 | 2 | **INTERRUPT** as it reaches the last two or three digits |
| +0:47 | 3 | `PARTIALLY_HEARD`, number struck through, consent blocked. It re-reads from the start |
| +1:45 | — | `/record/{live-id}` — "Consent permitted: no, blocked by identity" |
| +2:25 | 4 | The green-room record: REFUSED, callback, sha256, provenance |
| +2:55 | — | `evidence_ab.wav` full screen. **Play it. Say nothing over it** |

### The silence after the tap — know about it or you will panic

**~5 seconds of nothing** between the tap and the first Hindi word. Measured: 1.8s to open
the Rime socket and its warm spare, 2.7s for the first segment to synthesize, plus the
LiveKit join. It is not broken and it is not the wifi. Fill it:

> "It's opening a websocket to Rime and synthesizing her first sentence at eight
> kilohertz. That pause is the only one in the call — every later clause is synthesized
> while the previous one is still playing."

### The interrupt — by ear, never by counting

The clause measured **8.97s, 9.29s and 9.45s on three consecutive synthesis calls.** Rime
is not byte-identical run to run. **Cut as it speaks the last two or three digits.**

- Too early (under ~1s) → 0% heard. Technically correct, rhetorically dead.
- Too late (past the end) → the number counts as HEARD and you have no demo.
- `make e2e` cuts at 8.8s of 9.5s and records **92.7% heard, value NOT heard.**

This is the only thing in the whole talk a document cannot teach you. **Rehearse it once.**

---

## 4 · Your lines

Generated and grader-checked by `make answers` — re-run it if you change the document,
because these numbers come out of `demo/demo_kfs.pdf` and nowhere else.

The agent asks `{clause title} के बारे में आपने क्या समझा?` after each clause.

| # | It asks about | You say |
|---|---|---|
| 1 | लोन की पहचान | *(you interrupt this one — never answered on stage)* |
| 2 | मंज़ूर रकम | मुझे **पैंसठ हज़ार रुपए** मिलेंगे। |
| 3 | लोन की अवधि | **अड़तालीस महीने**, यानी **चार साल**। |
| 4 | महीने की किस्त | हर महीने **एक हज़ार नौ सौ छः रुपए**, **अड़तालीस** महीने तक। |
| 5 | ब्याज दर | ब्याज दर **सत्रह दशमलव नौ प्रतिशत** है। |
| 6 | फीस और शुल्क | प्रोसेसिंग फीस **दो हज़ार पाँच सौ रुपए**, बीमा **दो हज़ार चार सौ रुपए**, और लेट पेमेंट **दो प्रतिशत**। |
| 7 | सालाना कुल दर | सालाना कुल दर **बाईस दशमलव तीन आठ प्रतिशत** है। |
| 8 | कुल चुकाने की रकम | कुल **इक्यानवे हज़ार चार सौ अठासी रुपए** चुकाने होंगे। |
| 9 | लोन जल्दी बंद करना | **सात** दिन के अंदर बिना जुर्माने के बंद कर सकते हैं। |
| 10 | शिकायत | शिकायत का नंबर **एक आठ शून्य शून्य दो शून्य शून्य दो तीन दो दो** है। |
| — | क्या आप इन शर्तों पर सहमत हैं? | **हाँ, मैं सहमत हूँ।** |

For your green-room run, clause 1 is:

> मेरा खाता नंबर **शून्य शून्य शून्य दो चार आठ सात एक शून्य चार शून्य एक चार आठ तीन** है।

### Reading her screen

You never need to press anything to talk. The page tells you when it is your turn:

| Screen shows | Means |
|---|---|
| clause title + value, **stop button** below | the agent is reading — tap stop to barge in |
| **बोलिए** + a counting-down ring, no stop button | your turn. Just speak |
| ring turns **red** at 5s | answer now or it grades as unanswered |
| block on the **left**, amber bar | the **full clause text**, every segment, kept on screen while you answer |
| grey line under it | the agent's question |
| **boxed phrase** under the countdown | the one value you must say back |
| **«your words»** on the **right** | what Sarvam heard — green if the clause passed, amber if not |

The stop button is hidden while it is your turn, deliberately — there is nothing playing
to stop, and a big red button there reads as "press to speak". It is barge-in only.

The transcript is your best on-stage signal: if it shows something other than what you
said, that is Sarvam mishearing you, not the grader being wrong. Say the line again.

You get **30 seconds** per answer. A timeout grades as `not_mentioned`, which fails the
clause exactly as a wrong answer would.

### Two things that will bite you

Every clause now carries **exactly one** value, so there is only ever one thing to say —
the boxed phrase on screen. Fees used to be a single clause holding every charge, which
failed all of them when you forgot the last one.

1. **Wait ~2 seconds before saying हाँ.** An instant yes is refused as a reflex —
   `agent/rushed_consent.py`, `MIN_DELIBERATION_S = 1.5`. It is a feature and it will fire
   on you.
2. **Speak digits individually and unhurried** — शून्य · शून्य · शून्य · दो · चार — not as
   a run. Same reason the delivery layer exists.

Every line above is pre-checked against `agent.teachback.grade_offline`, the strict grader.
**Sarvam hearing you correctly is not guaranteed** — that is live ASR on your voice in a
noisy room, and it is the one link in the chain no test covers. If it mishears, say the
line again: the FSM allows two attempts per clause before it moves on.

---

## 5 · When it breaks

Decide fast and keep talking. The room forgives a failure; it does not forgive two minutes
of silent clicking.

| What died | What you do | What you say |
|---|---|---|
| Tap does nothing | `make agent` is down. Go to tab 3, click **▶ DEMO MODE** | "The network is gone, so this is a scripted call — but through the same ingest path, the same state machine and the same hashing code. The record will say `mode: demo`, because it would be dishonest if it didn't." |
| Page says "not secure" | You opened a LAN IP, not localhost | Re-open at `http://localhost:8000/c/{id}` |
| Mic denied | Click the mic button once more, then stop fighting it | Skip to tab 4 and play `evidence_ab.wav` |
| Rime 502 mid-call | Keep going, it recovers | "That's the warm-spare path — a 502 used to kill the call three clauses in. It doesn't any more." |
| Sarvam mishears you | Say the line again | *(nothing — it is a normal retry)* |
| Everything gone | `video/samjha_demo.mp4` from 0:28 | "This is a recording of the same flow. The barge-in is at thirty seconds." |
| You are at 6:00 still demoing | Stop mid-sentence, jump to slide 7 | The n caveat is the one thing you cannot skip |

**DEMO MODE only runs while the panel is open.** `POST /demo` alone does nothing — the
replay is driven by the panel's WebSocket on purpose, because "an asyncio task spawned
from a sync handler dies with the request's event loop". Click the button on `/`.

It runs 44.9s and ends in `CONSENT REFUSED` with a real hash. Verified.

---

## 6 · Afterwards

```bash
# unset SAMJHA_SKIP_INTRO in .env before anything a real borrower hears
```

Two lines to say unprompted during the talk, because they score:

- *"We measure and demo over a **simulated** telephone channel. We have not validated a
  live PSTN leg."*
- *"The Hindi has **not** been reviewed by a native speaker."* Say it before a judge who
  speaks Hindi says it for you.
