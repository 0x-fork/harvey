"""The Sender claims each email before sending it.

A reply, bounce, hand send or rejection can cancel an approved email while the
Sender is working through its queue. Without a claim the Sender sends from the
copy it read earlier and then overwrites the cancel with 'sent'. With one,
an email goes out only if it was still approved the moment the Sender took it,
and only once, however many senders run.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from harvey.state import MANUAL_SEND_KEY
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _approved(state, pid, step=2):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello there",
        body="A short, plain note. Question?", send_at=_now_iso(),
        status="approved", campaign_id="c1", step=step,
    )


async def _reply_lands(state, pid):
    # What the Handler does when a reply arrives: status first, then cancel.
    await state.update_prospect_status(pid, "replied")
    await state.cancel_pending_outbox_for_prospect(pid)


@pytest.mark.asyncio
async def test_a_cancel_after_the_checks_is_never_overwritten_by_a_send(state, monkeypatch):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)
    real_claim = state.claim_outbox_item

    async def claim_after_reply(item_id):
        # The reply is processed after the Sender's checks, before its claim.
        await _reply_lands(state, pid)
        return await real_claim(item_id)
    monkeypatch.setattr(state, "claim_outbox_item", claim_after_reply)

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "cancelled" and item["error"] == "stop_on_reply"


@pytest.mark.asyncio
async def test_a_reply_recorded_just_after_the_claim_still_stops_the_send(state, monkeypatch):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)
    real_claim = state.claim_outbox_item

    async def claim_then_reply(item_id):
        claimed = await real_claim(item_id)
        await _reply_lands(state, pid)  # the cancel can't touch a claimed row
        return claimed
    monkeypatch.setattr(state, "claim_outbox_item", claim_then_reply)

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "cancelled"
    assert "replied" in item["error"]


@pytest.mark.asyncio
async def test_a_hand_send_recorded_just_after_the_claim_still_stops_the_send(state, monkeypatch):
    pid = await seed_prospect(state, status="queued")
    item_id = await _approved(state, pid, step=1)
    real_claim = state.claim_outbox_item

    async def claim_then_hand_send(item_id):
        claimed = await real_claim(item_id)
        await state.set_setting(MANUAL_SEND_KEY + pid, "queued")
        return claimed
    monkeypatch.setattr(state, "claim_outbox_item", claim_then_hand_send)

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox_item(item_id))["error"] == "sent_by_hand"


@pytest.mark.asyncio
async def test_a_bounce_recorded_just_after_the_claim_still_stops_the_send(state, monkeypatch):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)
    real_claim = state.claim_outbox_item

    async def claim_then_bounce(item_id):
        claimed = await real_claim(item_id)
        prospect = await state.get_prospect(pid)
        await state.update_prospect_email(pid, prospect.email, "invalid")
        return claimed
    monkeypatch.setattr(state, "claim_outbox_item", claim_then_bounce)

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    assert (await state.get_outbox_item(item_id))["status"] == "failed"


@pytest.mark.asyncio
async def test_two_senders_at_once_send_each_email_once(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)

    class SlowProvider(FakeProvider):
        async def send_email(self, *a, **kw):
            await asyncio.sleep(0.05)  # both drains are inside the loop now
            return await super().send_email(*a, **kw)
    provider = SlowProvider()
    await asyncio.gather(
        make_sender(state, provider)._drain_due(),
        make_sender(state, provider)._drain_due(),
    )
    assert len(provider.sent) == 1
    assert len(await state.get_outbox(status="sent")) == 1


@pytest.mark.asyncio
async def test_claim_takes_only_approved_items(state):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)
    assert await state.claim_outbox_item(item_id) is True
    assert await state.claim_outbox_item(item_id) is False  # already taken
    assert (await state.get_outbox_item(item_id))["status"] == "sending"
    # Nothing cancels an email that is already on its way.
    assert await state.cancel_pending_outbox_for_prospect(pid) == 0


@pytest.mark.asyncio
async def test_a_send_that_raises_is_failed_not_retried(state):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)

    class BrokenProvider(FakeProvider):
        async def send_email(self, *a, **kw):
            raise RuntimeError("connection reset")
    await make_sender(state, BrokenProvider())._drain_due()
    item = await state.get_outbox_item(item_id)
    # It may have gone out before the error, so it is never sent again.
    assert item["status"] == "failed"
    assert "connection reset" in item["error"]


@pytest.mark.asyncio
async def test_an_email_stuck_sending_is_failed_after_a_while_never_resent(state):
    pid = await seed_prospect(state, status="contacted")
    stuck = await _approved(state, pid)
    fresh = await _approved(state, pid, step=3)
    await state.claim_outbox_item(stuck)
    await state.claim_outbox_item(fresh)
    long_ago = (datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1)).isoformat()
    async with state._connect() as db:  # backdate the claim (Harvey crashed)
        await db.execute("UPDATE outbox SET updated_at = ? WHERE id = ?", (long_ago, stuck))
        await db.commit()

    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent == []
    stuck_item = await state.get_outbox_item(stuck)
    assert stuck_item["status"] == "failed"
    assert "Sent folder" in stuck_item["error"]
    # A claim this recent may be another sender's email in flight.
    assert (await state.get_outbox_item(fresh))["status"] == "sending"
