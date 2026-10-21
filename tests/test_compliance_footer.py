"""Cold email carries a postal address and an opt-out line (CAN-SPAM), and
replies are read without the quoted email they answer.

Based on community PR #13 by Camilo Cruz.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey.agents.sender import Sender
from harvey.config import ComplianceConfig, EnvConfig, compliance_notice
from harvey.gate import pre_send_check
from harvey.integrations.mail_provider import InboundMessage, strip_quoted_reply
from harvey.models.campaign import Campaign, EmailStep
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, StubBrain, make_handler, make_sender, seed_prospect, state,
)

ADDRESS = "Acme Co, 100 Main St, Austin, TX 78701"
LINE = ComplianceConfig().opt_out_line


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _approved(state, pid, kind="sequence", body="A short, plain note. Question?"):
    return await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com", subject="hello there",
        body=body, send_at=_now_iso(), status="approved",
        campaign_id="c1" if kind == "sequence" else "", step=2, kind=kind,
    )


def _sender(state, provider, address=ADDRESS, line=LINE):
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(postal_address=address, opt_out_line=line)
    return sender


# ── The footer ──

@pytest.mark.asyncio
async def test_cold_email_goes_out_with_the_address_and_opt_out_line(state):
    pid = await seed_prospect(state, status="contacted")
    item_id = await _approved(state, pid)
    provider = FakeProvider()
    await _sender(state, provider)._drain_due()
    assert provider.sent[0]["body"] == (
        "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE)
    # The Outbox keeps what actually went out.
    assert (await state.get_outbox_item(item_id))["body"] == provider.sent[0]["body"]


@pytest.mark.asyncio
async def test_replies_get_no_footer(state):
    pid = await seed_prospect(state, status="replied")
    await _approved(state, pid, kind="reply", body="Sure, Tuesday works. Talk then?")
    provider = FakeProvider()
    await _sender(state, provider)._drain_due()
    assert provider.sent[0]["body"] == "Sure, Tuesday works. Talk then?"


@pytest.mark.asyncio
async def test_without_an_address_cold_email_still_goes_with_the_opt_out_line(state, caplog):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    await _sender(state, provider, address="")._drain_due()
    assert provider.sent[0]["body"] == "A short, plain note. Question?\n\n" + LINE
    assert "postal_address" in caplog.text


@pytest.mark.asyncio
async def test_both_lines_can_be_turned_off(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    await _sender(state, provider, address="", line="")._drain_due()
    assert provider.sent[0]["body"] == "A short, plain note. Question?"


@pytest.mark.asyncio
async def test_a_footer_already_in_the_body_is_not_added_twice(state):
    pid = await seed_prospect(state, status="contacted")
    body = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
    await _approved(state, pid, body=body)
    provider = FakeProvider()
    await _sender(state, provider)._drain_due()
    assert provider.sent[0]["body"] == body


@pytest.mark.asyncio
async def test_old_configs_without_a_compliance_section_still_send(state):
    # Test configs (and any hand-built config) may lack the section entirely.
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid)
    provider = FakeProvider()
    await make_sender(state, provider)._drain_due()
    assert provider.sent[0]["body"].endswith(LINE)


def test_the_gate_refuses_cold_email_missing_its_configured_address():
    ok = pre_send_check("jane@acme.com", "hello there", "A note. Question?",
                        postal_address=ADDRESS)
    assert not ok and any("postal address" in r for r in ok.reasons)
    assert pre_send_check("jane@acme.com", "hello there",
                          "A note. Question?\n\n" + ADDRESS, postal_address=ADDRESS)
    # Replies never need it.
    assert pre_send_check("jane@acme.com", "hello there", "Sure, Tuesday works.",
                          kind="reply", postal_address=ADDRESS)


@pytest.mark.asyncio
async def test_instantly_sequences_carry_the_footer(state):
    config = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=False,
            max_daily_sends=50, send_to_risky=False)),
        compliance=ComplianceConfig(postal_address=ADDRESS),
    )
    sender = Sender(None, state, config, EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.set_campaign_emails.return_value = None  # stop after the upload
    campaign = Campaign(id="c", name="Draft", instantly_campaign_id="existing",
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    await sender._deploy_campaign(campaign)
    uploaded = sender.instantly.set_campaign_emails.await_args.args[1]
    assert uploaded[0]["body"] == "Would this help?\n\n" + ADDRESS + "\n" + LINE


def test_the_notice_asks_for_an_address_only_when_email_is_on():
    def cfg(address, enabled=True):
        return SimpleNamespace(
            channels=SimpleNamespace(email=SimpleNamespace(enabled=enabled)),
            compliance=ComplianceConfig(postal_address=address))
    assert "compliance.postal_address" in compliance_notice(cfg(""))
    assert compliance_notice(cfg(ADDRESS)) == ""
    assert compliance_notice(cfg("", enabled=False)) == ""
    assert compliance_notice(cfg("   ")) != ""


# ── Reading replies ──

GMAIL_QUOTE = (
    "Sounds interesting, send me pricing.\n\n"
    "On Tue, Oct 14, 2026 at 3:02 PM Harvey <harvey@x.co> wrote:\n"
    "> A short, plain note. Question?\n>\n> " + ADDRESS + "\n> " + LINE
)
OUTLOOK_QUOTE = (
    "Sounds interesting, send me pricing.\n\n"
    "From: Harvey <harvey@x.co>\nSent: Tuesday, October 14, 2026 3:02 PM\n"
    "To: Jane\nSubject: hello there\n\n"
    "A short, plain note. Question?\n\n" + ADDRESS + "\n"
    # Outlook re-wraps lines and uses curly quotes.
    "Not interested? Reply “no\nthanks” and I won’t email again."
)


def test_strip_keeps_only_what_they_wrote():
    assert strip_quoted_reply(GMAIL_QUOTE) == "Sounds interesting, send me pricing."
    assert strip_quoted_reply(OUTLOOK_QUOTE) == "Sounds interesting, send me pricing."
    assert strip_quoted_reply(
        "Thanks.\n\n-----Original Message-----\nFrom: x") == "Thanks."
    # Answers written between quoted lines are theirs too.
    assert strip_quoted_reply(
        "Hi Harvey,\n\n> Would you be open to a call?\n\nNo. Remove me please."
    ) == "Hi Harvey,\n\nNo. Remove me please."
    # Everything under a quote header: keep their lines, never return nothing.
    assert "Remove me" in strip_quoted_reply(
        "On Tue Harvey wrote:\n> Open to a call?\nRemove me")
    assert strip_quoted_reply("") == ""


class RecordingBrain(StubBrain):
    def __init__(self, intent):
        super().__init__(intent=intent)
        self.prompts = []

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append(prompt)
        return await super().think(prompt, session_id=session_id, agent=agent, task=task)


async def _reply(state, body, intent="interested"):
    pid = await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com", subject="Re: hello there", body=body)]
    handler = make_handler(state, provider, intent=intent)
    handler.config.compliance = ComplianceConfig(postal_address=ADDRESS)
    handler.brain = RecordingBrain(intent)
    await handler._run_native()
    return (await state.get_prospect(pid)).status, handler.brain.prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    GMAIL_QUOTE, OUTLOOK_QUOTE,
    # An HTML-only reply converted to text keeps its entities.
    "Sounds interesting.\n\nOn Tue Harvey wrote:\nNot interested? Reply &quot;no thanks&quot; "
    "and I won&#39;t email again.",
    # Gmail re-wraps long quoted lines.
    "Sounds interesting.\n\nOn Tue Harvey wrote:\n> Not interested? Reply \"no\n> thanks\" "
    "and I won't email again.",
], ids=["gmail", "outlook", "html-entities", "gmail-wrapped"])
async def test_our_own_quoted_footer_never_reads_as_an_opt_out(state, body):
    status, prompts = await _reply(state, body)
    assert status != "opted_out"
    # The model reads their words, not our quoted email.
    assert prompts and all(ADDRESS not in p for p in prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "No thanks.",
    "no thank you\n\nOn Tue Harvey wrote:\n> A short, plain note.",
    # Answered inline, below and between our quoted lines.
    "> Would you be open to a quick call?\nNo. Please remove me from your list.",
    "Hi Harvey,\n\n> Would you be open to a quick call?\n\nNo. Remove me from your list.",
    "On Tue Harvey wrote:\n> Open to a call?\nunsubscribe",
], ids=["no-thanks", "above-quote", "below-quote", "between-quotes", "under-header"])
async def test_an_opt_out_is_honoured_wherever_they_wrote_it(state, body):
    status, _ = await _reply(state, body)
    assert status == "opted_out"


@pytest.mark.asyncio
async def test_a_legal_threat_under_the_quote_still_escalates(state):
    status, prompts = await _reply(
        state, "> A short, plain note.\nMy attorney will be in touch.")
    assert status != "opted_out"
    assert prompts == []  # escalated by keyword, no model call, no auto-reply


# ── Dashboard ──

from tests.test_dashboard_manual_send import client, env  # noqa: E402,F401


def _config(address):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(enabled=True)),
        compliance=ComplianceConfig(postal_address=address))


def test_the_outbox_shows_the_footer_and_asks_for_a_missing_address(client, monkeypatch):
    import harvey.config
    monkeypatch.setattr(harvey.config, "load_config", lambda: _config(""))
    data = client.get("/api/outbox").json()
    assert data["footer"] == LINE
    assert "postal_address" in data["compliance_notice"]

    monkeypatch.setattr(harvey.config, "load_config", lambda: _config(ADDRESS))
    data = client.get("/api/outbox").json()
    assert data["footer"] == ADDRESS + "\n" + LINE
    assert data["compliance_notice"] == ""


def test_an_unreadable_config_still_shows_the_outbox(client, monkeypatch):
    import harvey.config

    def broken():
        raise harvey.config.ConfigError("bad yaml")
    monkeypatch.setattr(harvey.config, "load_config", broken)
    data = client.get("/api/outbox").json()
    assert data["footer"] == "" and "pending" in data
