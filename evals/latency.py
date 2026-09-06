"""Latency measurement: TTFB (cold and warm, separately) and barge-in stop latency.

Two things are deliberately NOT conflated, because conflating them would be dishonest:

  * COLD vs WARM. A cold TTFB pays the full TLS + WebSocket handshake. A warm one is a
    flush on an already-open socket — which is what the product actually does, because it
    keeps a spare connection open. Reporting only the warm number would be a demo trick;
    reporting only the cold one would understate the product. Both, always.

  * NETWORK vs MODEL. Rime has no India region (US West/East only), so from India a
    trans-Pacific round trip dominates. We measure the bare TCP connect RTT to the same
    host separately and report the model+queue component as warm TTFB minus one RTT. A
    single "TTFB" number measured from India would be mostly geography.

Barge-in stop latency measures the only hard stop Rime has. There is no cancel primitive:
`clear` does not cancel in-flight synthesis and does not drop already-flushed text, and
`contextId` is not a fence (it came back null on every frame in our battery). So a hard
stop is: stop client playout, send `clear`, and CLOSE THE SOCKET. What is timed here is the
transport half — request-to-stop until the socket is closed and no further audio can
arrive. Client-side playout stop is instantaneous and is measured in the agent, not here.

    uv run python evals/latency.py --trials 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import websockets  # noqa: E402

from delivery import rime_ws3  # noqa: E402

RESULTS = Path(__file__).resolve().parents[1] / "evals/results"

# One sentence, held constant across every trial. Long enough that synthesis is still
# in flight when the barge-in fires — a stop measured after `done` measures nothing.
PROBE = "आपके लोन की कुल स्वीकृत राशि एक लाख पच्चीस हज़ार रुपए है और वार्षिक ब्याज दर अठारह दशमलव पाँच प्रतिशत है।"

HOST = rime_ws3.WS3.split("://")[1].split("/")[0]


def tcp_rtt_ms(host: str, port: int = 443) -> float:
    """Bare TCP connect time. The geographic floor under every other number here."""
    t0 = time.perf_counter()
    with socket.create_connection((host, port), timeout=10):
        return (time.perf_counter() - t0) * 1000


async def _first_chunk(ws, t0: float) -> tuple[float, int]:
    """Wait for the first audio chunk. Returns (ms since t0, bytes in that chunk)."""
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("type") == "chunk":
            payload = msg.get("data") or msg.get("audioContent") or ""
            return (time.perf_counter() - t0) * 1000, len(payload)


async def _drain_idle(ws, idle_s: float = 0.5, max_s: float = 30.0) -> int:
    """Read until the socket goes quiet. Returns chunks seen.

    Necessary, not decorative: /ws3 emits no per-flush completion event (the battery saw
    only chunk/timestamps/done), so the ONLY way to know a warm socket is idle is silence.
    Skip this and the "warm TTFB" you measure is a chunk of the previous utterance that was
    already sitting in the receive queue — we measured 0.4 ms that way, which is nonsense.
    """
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < max_s:
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), idle_s))
        except (asyncio.TimeoutError, websockets.ConnectionClosed):
            return n
        if msg.get("type") == "chunk":
            n += 1
    return n


async def measure_ttfb(trials: int) -> dict:
    """Cold = fresh socket per trial. Warm = a flush on a socket that is open and idle."""
    cold, connect, warm = [], [], []
    headers = {"Authorization": f"Bearer {rime_ws3.api_key()}"}

    for _ in range(trials):
        t0 = time.perf_counter()
        async with websockets.connect(rime_ws3.url(), additional_headers=headers,
                                      max_size=None) as ws:
            connect.append((time.perf_counter() - t0) * 1000)
            await ws.send(json.dumps({"text": PROBE}))
            await ws.send(json.dumps({"operation": "flush"}))
            cold.append((await _first_chunk(ws, t0))[0])

            await _drain_idle(ws)
            t1 = time.perf_counter()
            await ws.send(json.dumps({"text": PROBE}))
            await ws.send(json.dumps({"operation": "flush"}))
            warm.append((await _first_chunk(ws, t1))[0])

    return {"cold_ms": cold, "warm_ms": warm, "connect_ms": connect}


def _hard_stop(ws) -> None:
    """Abort the TCP connection. This is the product's barge-in, and it is local: no close
    handshake, because waiting for the server to agree is exactly the latency we refuse to
    pay. A graceful `close()` here measured 10 s — the server keeps streaming and the
    handshake times out."""
    transport = getattr(ws, "transport", None)
    if transport is not None:
        transport.abort()


async def measure_bargein(trials: int, cut_after_ms: float) -> dict:
    stops, played = [], []
    headers = {"Authorization": f"Bearer {rime_ws3.api_key()}"}

    for _ in range(trials):
        async with websockets.connect(rime_ws3.url(), additional_headers=headers,
                                      max_size=None) as ws:
            await ws.send(json.dumps({"text": PROBE}))
            await ws.send(json.dumps({"operation": "flush"}))
            t0 = time.perf_counter()
            n = 0
            while (time.perf_counter() - t0) * 1000 < cut_after_ms:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                except asyncio.TimeoutError:
                    break
                if msg.get("type") == "chunk":
                    n += len(msg.get("data") or "")
                elif msg.get("type") in ("done", "error"):
                    break

            t_stop = time.perf_counter()
            await ws.send(json.dumps({"operation": "clear"}))
            _hard_stop(ws)
            await ws.wait_closed()
            stops.append((time.perf_counter() - t_stop) * 1000)
            played.append(n)

    return {"stop_ms": stops, "bytes_before_cut": played}


async def probe_clear_does_not_cancel(listen_s: float = 1.5) -> dict:
    """Send `clear` mid-synthesis and keep listening. Rime's docs say clear does not cancel
    in-flight synthesis; this is the measurement, and it is why the stop above closes the
    socket instead of trusting the operation."""
    headers = {"Authorization": f"Bearer {rime_ws3.api_key()}"}
    async with websockets.connect(rime_ws3.url(), additional_headers=headers,
                                  max_size=None) as ws:
        await ws.send(json.dumps({"text": PROBE}))
        await ws.send(json.dumps({"operation": "flush"}))
        before = 0
        t0 = time.perf_counter()
        while (time.perf_counter() - t0) < 0.8:
            try:
                if json.loads(await asyncio.wait_for(ws.recv(), 5)).get("type") == "chunk":
                    before += 1
            except asyncio.TimeoutError:
                break

        await ws.send(json.dumps({"operation": "clear"}))
        after, t1 = 0, time.perf_counter()
        while (time.perf_counter() - t1) < listen_s:
            try:
                if json.loads(await asyncio.wait_for(ws.recv(), listen_s)).get("type") == "chunk":
                    after += 1
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
        _hard_stop(ws)
        return {"chunks_before_clear": before, "chunks_after_clear": after,
                "listen_window_s": listen_s}


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile. n is small by design; the raw samples are also reported."""
    if not xs:
        return float("nan")
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * len(s) + 0.5) - 1))]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trials", type=int, default=5)
    p.add_argument("--cut-after-ms", type=float, default=800.0,
                   help="fire the barge-in this long after the first flush")
    args = p.parse_args()

    rtts = [tcp_rtt_ms(HOST) for _ in range(args.trials)]
    ttfb = asyncio.run(measure_ttfb(args.trials))
    barge = asyncio.run(measure_bargein(args.trials, args.cut_after_ms))
    clear_probe = asyncio.run(probe_clear_does_not_cancel())

    net = statistics.median(rtts)
    warm_p50 = statistics.median(ttfb["warm_ms"])
    out = {
        "host": HOST,
        "endpoint_ip": socket.gethostbyname(HOST),
        "trials": args.trials,
        "request": {k: PARAM for k, PARAM in
                    (("modelId", rime_ws3.MODEL_ID), ("speaker", rime_ws3.SPEAKER),
                     ("lang", rime_ws3.LANG), ("samplingRate", rime_ws3.SAMPLE_RATE),
                     ("audioFormat", rime_ws3.AUDIO_FORMAT))},
        "network": {"tcp_rtt_ms_p50": round(net, 1), "samples": [round(x, 1) for x in rtts],
                    "note": "Rime has no India region. This is the geographic floor, not "
                            "model time."},
        "ttfb_cold_ms": {"p50": round(statistics.median(ttfb["cold_ms"]), 1),
                         "p95": round(pct(ttfb["cold_ms"], 0.95), 1),
                         "samples": [round(x, 1) for x in ttfb["cold_ms"]],
                         "note": "includes TLS + WebSocket handshake"},
        "ttfb_warm_ms": {"p50": round(warm_p50, 1),
                         "p95": round(pct(ttfb["warm_ms"], 0.95), 1),
                         "samples": [round(x, 1) for x in ttfb["warm_ms"]],
                         "note": "flush on an already-open socket — the product's path"},
        "connect_ms_p50": round(statistics.median(ttfb["connect_ms"]), 1),
        "model_ms_p50_estimate": round(warm_p50 - net, 1),
        "model_ms_note": "warm TTFB minus one TCP RTT. An estimate of the non-network "
                         "component, reported separately so geography is not sold as model "
                         "speed. Rime's published 96 ms P50 is a self-hosted H100 number.",
        "bargein_stop_ms": {"p50": round(statistics.median(barge["stop_ms"]), 1),
                            "p95": round(pct(barge["stop_ms"], 0.95), 1),
                            "samples": [round(x, 1) for x in barge["stop_ms"]],
                            "method": "send clear, then CLOSE the socket. `clear` does not "
                                      "cancel in-flight synthesis and contextId is not a "
                                      "fence, so closing is the only hard stop.",
                            "b64_bytes_received_before_cut": barge["bytes_before_cut"]},
        "clear_does_not_cancel": clear_probe,
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "latency.json"
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(f"endpoint {HOST} ({out['endpoint_ip']}), {args.trials} trials\n")
    print(f"  network TCP RTT   p50 {out['network']['tcp_rtt_ms_p50']:>8.1f} ms")
    print(f"  TTFB cold         p50 {out['ttfb_cold_ms']['p50']:>8.1f} ms   "
          f"p95 {out['ttfb_cold_ms']['p95']:.1f} ms")
    print(f"  TTFB warm         p50 {out['ttfb_warm_ms']['p50']:>8.1f} ms   "
          f"p95 {out['ttfb_warm_ms']['p95']:.1f} ms")
    print(f"  model (warm-RTT)  p50 {out['model_ms_p50_estimate']:>8.1f} ms")
    print(f"  barge-in stop     p50 {out['bargein_stop_ms']['p50']:>8.1f} ms   "
          f"p95 {out['bargein_stop_ms']['p95']:.1f} ms")
    print(f"\n  `clear` probe: {clear_probe['chunks_before_clear']} chunks before, "
          f"{clear_probe['chunks_after_clear']} chunks arrived AFTER clear "
          f"in {clear_probe['listen_window_s']}s — clear is not a cancel.")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
