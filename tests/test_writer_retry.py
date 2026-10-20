"""A batch the model can't write for must not loop forever.

When _write_sequence comes back empty (unparseable output, or an inbox
sequence that used a person's name), the batch's prospects stayed 'new', so
every heartbeat chose write_campaign again, spent a Claude call, and Scout
never ran. Now each prospect gets a few attempts; after that it is moved to
'write_failed', which is visible and no longer blocks the pipeline. A
provider outage is not the model's fault and never counts as an attempt.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.agents.writer import MAX_WRITE_ATTEMPTS, Writer
from harvey.main import decide_next_action
from harvey.models.prospect import Prospect
from harvey.state import StateManager

GOOD = [{"step": 1, "subject": "quick one", "body": "Hi {{first_name}}, who answers at {{company}}?",
         "delay_days": 0}]


class FakeBrain:
    def __init__(self, reply=None, paused=""):
        self.reply = reply
        self.paused_reason = paused
        self.calls = 0

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.calls += 1
        return None if self.paused_reason else self.reply


CONFIG = SimpleNamespace(
    channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=False)),
    product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
    persona=SimpleNamespace(name="S", company="C", role="R", tone="t"),
)


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "w.db"))
    await state.init_db()
    return state


async def add(state, n):
    return await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        industry="Roofing", email=f"p{n}@co{n}.test", email_status="verified",
    ))


async def status(state, pid):
    return (await state.get_prospect(pid)).status


@pytest.mark.asyncio
async def test_a_batch_that_keeps_failing_stops_after_a_few_attempts(state):
    pid = await add(state, 1)
    brain = FakeBrain(reply="not a sequence")
    writer = Writer(brain, state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS - 1):
        await writer.run()
        assert await status(state, pid) == "new"
    await writer.run()
    assert await status(state, pid) == "write_failed"
    # The heartbeat moves on to prospecting instead of writing again.
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"
    calls = brain.calls
    await writer.run()
    assert brain.calls == calls  # no more model calls for it


@pytest.mark.asyncio
async def test_a_provider_outage_never_counts_as_an_attempt(state):
    pid = await add(state, 1)
    writer = Writer(FakeBrain(paused="Claude usage limit reached"), state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS + 2):
        await writer.run()
    assert await status(state, pid) == "new"


@pytest.mark.asyncio
async def test_success_after_a_failure_writes_normally(state):
    pid = await add(state, 1)
    brain = FakeBrain(reply="not a sequence")
    writer = Writer(brain, state, CONFIG, None)
    await writer.run()
    brain.reply = GOOD
    await writer.run()
    assert await status(state, pid) == "queued"


@pytest.mark.asyncio
async def test_attempts_are_counted_per_prospect(state):
    # Someone who joins a failing batch late gets their own full set of tries.
    early = await add(state, 1)
    writer = Writer(FakeBrain(reply="not a sequence"), state, CONFIG, None)
    for _ in range(MAX_WRITE_ATTEMPTS - 1):
        await writer.run()
    late = await add(state, 2)
    await writer.run()
    assert await status(state, early) == "write_failed"
    assert await status(state, late) == "new"
