"""A follow-up never goes out before, or too soon after, the email it follows.

Sequence emails are staged with send times days apart. If step 1 is held
(awaiting approval, sending paused, a compliance hold, a warmup ramp), the
follow-ups' scheduled times still arrive; without spacing, approving late
sends the whole sequence at once, or step 2 before step 1.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_campaign, seed_prospect, state,
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_approving_a_week_late_sends_step_one_only(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])           # steps 1 and 2, 3 days apart
    sender = make_sender(state, FakeProvider())
    await sender._run_native()                   # staged, awaiting approval
    week_ago = _now() - timedelta(days=7)
    for item in await state.get_outbox(status="pending_review"):
        await state.update_outbox_item(
            item["id"], send_at=(week_ago + timedelta(days=3 * (item["step"] - 1))).isoformat())
    await state.approve_outbox()                 # "Approve all", a week late

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert [m["subject"] for m in provider.sent] == ["hi Jane"]   # step 1 only
    step2 = next(i for i in await state.get_outbox(status="approved") if i["step"] == 2)
    assert step2  # waits its 3 days after step 1 actually went out


@pytest.mark.asyncio
async def test_a_follow_up_whose_first_email_never_went_out_waits(state):
    pid = await seed_prospect(state, status="queued")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s1", body="One. Question?",
        send_at=(_now() + timedelta(days=1)).isoformat(), status="approved",
        campaign_id="c1", step=1)
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="s2", body="Two. Question?",
        send_at=(_now() - timedelta(minutes=1)).isoformat(), status="approved",
        campaign_id="c1", step=2)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
