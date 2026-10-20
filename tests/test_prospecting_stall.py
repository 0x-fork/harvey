"""Prospects the Writer can't use must not stop Harvey from prospecting.

The Writer only drafts for verified or risky addresses; a guessed, invalid or
missing email leaves the prospect 'new'. The heartbeat used to count every
'new' prospect, so a single guessed address made it choose write_campaign
every cycle, the Writer had nothing to do, and Scout never ran again.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.main import decide_next_action
from harvey.models.prospect import Prospect
from harvey.state import StateManager

CONFIG = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(
    provider="gmail", require_approval=True)))


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "stall.db"))
    await state.init_db()
    return state


async def add(state, n, email, email_status):
    await state.add_prospect(Prospect(
        first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co{n}",
        email=email, email_status=email_status,
    ))


@pytest.mark.asyncio
async def test_summary_counts_only_prospects_the_writer_can_use(state):
    await add(state, 1, "a@one.test", "verified")
    await add(state, 2, "b@two.test", "risky")
    await add(state, 3, "c@three.test", "guess")
    await add(state, 4, "d@four.test", "invalid")
    await add(state, 5, "", "")
    summary = await state.get_state_summary()
    assert summary["prospects"]["new"] == 5
    assert summary["writable_prospects"] == 2


@pytest.mark.asyncio
async def test_guessed_emails_alone_lead_back_to_prospecting(state):
    for n in range(3):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_many_guessed_emails_still_lead_back_to_prospecting(state):
    # 20+ unusable prospects must not tip Harvey into idle either.
    for n in range(25):
        await add(state, n, f"p{n}@co{n}.test", "guess")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "prospect"


@pytest.mark.asyncio
async def test_a_deliverable_prospect_is_still_written_for(state):
    await add(state, 1, "p1@co1.test", "guess")
    await add(state, 2, "p2@co2.test", "verified")
    summary = await state.get_state_summary()
    assert await decide_next_action(None, state, CONFIG, summary=summary) == "write_campaign"


@pytest.mark.asyncio
async def test_a_summary_without_the_new_count_keeps_the_old_rule():
    # Callers that build their own summary (older tests, scripts) still work.
    summary = {"prospects": {"new": 3}, "draft_campaigns": 0, "open_conversations": 0}
    assert await decide_next_action(None, None, CONFIG, summary=summary) == "write_campaign"
