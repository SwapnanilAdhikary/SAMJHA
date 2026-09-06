"""Entrypoint. livekit-agents 1.8.0: AgentServer + @server.rtc_session() + cli.run_app().

    uv run python -m agent.main dev       # connect to LIVEKIT_URL and wait for a call
    uv run python -m agent.main --help

The LIVEKIT_API_SECRET in .env is a masked placeholder, so this has NOT been run against a
live room. The logic it drives — the FSM, the grader, the rushed-consent gate, the mu-law
decoder and the whole clause loop — is verified offline in tests/test_agent.py against
fakes, which is where the correctness that matters actually lives.

Clauses come from the real pipeline: a synthetic KFS fixture -> `kfs.build_clauses`, which
is where one-key-value-per-segment is enforced. ALL DATA SYNTHETIC — no real borrower data,
ever. Point `SAMJHA_KFS` at a different fixture to read a different loan.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import AgentServer, JobContext, cli

from agent.session import (
    ConsentAgent,
    PlayoutLedger,
    RimeSocketPool,
    build_session,
    new_fsm,
    run_consent_flow,
    wire_transcripts,
)
from kfs.build_clauses import build_clauses
from kfs.clauses import Clause
from kfs.schema import KFS

load_dotenv()
logger = logging.getLogger("samjha.agent")

DEFAULT_KFS = "fixtures/synthetic/kfs_02_personal.json"

server = AgentServer()


def load_clauses(path: str | None = None) -> list[Clause]:
    """Read a synthetic KFS and build its clauses.

    The identifier clause reads first, which is not cosmetic: `account_identifier` is the
    category the day-1 pilot found Rime reads as an Indian-scale quantity rather than a
    digit sequence (raw 1/5 vs delivery-layer 5/5). It is the claim the demo has to show.
    """
    src = Path(path or os.environ.get("SAMJHA_KFS", DEFAULT_KFS))
    return build_clauses(KFS.model_validate_json(src.read_text(encoding="utf-8")))


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    clauses = load_clauses()
    call_id = ctx.room.name or "unknown-room"
    fsm = new_fsm(clauses, call_id)

    session = build_session()
    answers = wire_transcripts(session)
    ledger = PlayoutLedger()

    pool = RimeSocketPool()
    await pool.start()  # opens the live socket AND the warm spare before anyone speaks

    try:
        await session.start(ConsentAgent(), room=ctx.room)
        ledger.attach(session.output.audio)

        decision = await run_consent_flow(session, fsm, clauses, pool, ledger, answers)

        if decision is None:
            logger.info("call ended with no consent utterance", extra={"call_id": call_id})
        elif decision.granted:
            await session.say("धन्यवाद। आपकी सहमति दर्ज कर ली गई है।").wait_for_playout()
        else:
            # The refusal is already in events/{call_id}.jsonl. Say so out loud too: a
            # borrower who is refused deserves to hear why, not just be hung up on.
            await session.say(
                "अभी सहमति दर्ज नहीं की जा सकती। हमारा प्रतिनिधि आपको फिर से कॉल करेगा।"
            ).wait_for_playout()
            logger.info("consent refused: %s", decision.reasons, extra={"call_id": call_id})
    finally:
        await pool.aclose()


if __name__ == "__main__":
    cli.run_app(server)
