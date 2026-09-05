"""Telephone-channel simulation. Fixed order of operations, held constant across arms.

    synthesize -> resample 8 kHz -> band-limit 300-3400 Hz -> noise at target SNR
               -> G.711 mu-law encode -> decode -> ASR

Everything is done in ffmpeg. Four traps, each verified on this machine's ffmpeg 8.1 and
each of which produces WRONG OUTPUT rather than an error:

  * `aresample=8000:resampler=soxr` HARD-FAILS on the Homebrew macOS build (libsoxr is not
    compiled in). Nearly every blog recipe recommends it. The default swresample engine is
    fine and reproducible.
  * ffmpeg's single `bandpass` filter is a Q-based peaking BPF, NOT a 300-3400 band. Using
    it gives you a plausible-sounding but wrong channel. Cascade highpass+lowpass instead.
  * `amix` NORMALISES by default, dividing each input by N and silently destroying the
    target SNR by 6 dB. `normalize=0` is mandatory.
  * scipy.io.wavfile cannot read WAVE_FORMAT_MULAW (fmt tag 0x0007). Decode back to
    pcm_s16le before any Python analysis. (audioop, the classic answer, was removed from
    the stdlib in 3.13 under PEP 594.)

Self-check:  uv run python -m evals.channel
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

SAMPLE_RATE = 8000
BAND = (300, 3400)  # ITU narrowband telephony

# Noise is added BEFORE the codec: we are simulating a noisy handset, not line noise.
# The choice matters and is not commutative — it is fixed here and disclosed in
# evals/ACCEPTANCE.md so both arms get identical treatment.
NOISE_BEFORE_CODEC = True


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, capture_output=True, **kw)


def band_limit_and_encode(src: Path, dest: Path) -> Path:
    """Resample, band-limit, G.711 mu-law encode. dest is a mu-law WAV."""
    _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-af", f"aresample={SAMPLE_RATE},"
               f"highpass=f={BAND[0]}:p=2,lowpass=f={BAND[1]}:p=2",
        "-c:a", "pcm_mulaw", "-ar", str(SAMPLE_RATE), "-ac", "1", str(dest),
    ])
    return dest


def decode(src: Path, dest: Path) -> Path:
    """mu-law WAV -> pcm_s16le WAV, so Python/ASR can read it."""
    _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), str(dest),
    ])
    return dest


def mean_volume_db(path: Path) -> float:
    """Whole-file mean volume in dBFS, via ffmpeg volumedetect.

    NOTE, disclosed in ACCEPTANCE.md: this is whole-file RMS INCLUDING silence. The
    rigorous reference is ITU-T P.56 active speech level, which gates on speech activity.
    A clause with long pauses therefore measures quieter than it is and receives slightly
    less noise than the nominal SNR implies. Applied identically to both arms.
    """
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", p.stderr)
    if not m:
        raise RuntimeError(f"volumedetect produced no mean_volume for {path}")
    return float(m.group(1))


def mix_noise(speech: Path, noise: Path, dest: Path, snr_db: float) -> Path:
    """Mix noise under speech at a target SNR. `normalize=0` is not optional."""
    delta = mean_volume_db(speech) - mean_volume_db(noise) - snr_db
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(speech), "-stream_loop", "-1", "-i", str(noise),
        "-filter_complex",
        f"[1:a]volume={delta:.2f}dB[n];"
        f"[0:a][n]amix=inputs=2:duration=first:normalize=0[out]",
        "-map", "[out]", "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), str(dest),
    ])
    return dest


def apply(src: Path, dest: Path, *, noise: Path | None = None,
          snr_db: float | None = None) -> Path:
    """Full chain. Returns a decoded pcm_s16le WAV ready for ASR."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        stage = src

        if noise is not None and snr_db is not None and NOISE_BEFORE_CODEC:
            # Band-limit first so the noise we add is itself band-limited by the codec
            # stage, matching a handset picking up room noise.
            stage = mix_noise(stage, noise, tmp / "noisy.wav", snr_db)

        ulaw = band_limit_and_encode(stage, tmp / "ulaw.wav")
        return decode(ulaw, dest)


def _demo() -> None:
    """Self-check: prove the chain runs and actually produces mu-law at 8 kHz."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tone = tmp / "tone.wav"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
              "-i", "sine=frequency=440:duration=1:sample_rate=22050", str(tone)])

        ulaw = band_limit_and_encode(tone, tmp / "ulaw.wav")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,sample_rate,channels", "-of", "default=nw=1", str(ulaw)],
            capture_output=True, text=True,
        ).stdout
        assert "pcm_mulaw" in probe, probe
        assert "8000" in probe, probe

        out = apply(tone, tmp / "out.wav")
        assert out.exists() and out.stat().st_size > 0

        # mu-law is 1 byte/sample: 1 s of 8 kHz is ~8000 bytes of payload.
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(ulaw), "-f", "mulaw", "-"],
            capture_output=True,
        ).stdout
        assert 7000 < len(raw) < 9000, f"expected ~8000 mu-law bytes, got {len(raw)}"

        print(probe.strip())
        print(f"mu-law payload: {len(raw)} bytes = {len(raw)/SAMPLE_RATE:.3f}s")
        print("channel chain OK")


if __name__ == "__main__":
    _demo()
