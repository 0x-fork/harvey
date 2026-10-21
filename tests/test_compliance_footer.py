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


# ── Review round 1: false opt-outs, missed opt-outs ──

async def _reply_after_sending(state, body, sent_bodies=(), intent="interested",
                               line=LINE):
    """A reply from someone Harvey already emailed `sent_bodies`."""
    pid = await seed_prospect(state, status="contacted")
    for n, sent in enumerate(sent_bodies, start=1):
        oid = await state.add_outbox_item(
            prospect_id=pid, to_email="jane@acme.com", subject="hello there",
            body=sent, send_at=_now_iso(), status="approved", campaign_id="c1", step=n)
        await state.update_outbox_item(oid, status="sent", sent_at=_now_iso())
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com", subject="Re: hello there", body=body)]
    handler = make_handler(state, provider, intent=intent)
    handler.config.compliance = ComplianceConfig(postal_address=ADDRESS, opt_out_line=line)
    handler.brain = RecordingBrain(intent)
    await handler._run_native()
    return (await state.get_prospect(pid)).status, handler.brain.prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Do you offer a monthly plan, yes or no\nThanks,\nBob",
    "Sounds good, call me.\n\nMario Bruno\nThanks for reaching out",
    "Interested! Can we talk next month? No Thanksgiving week though.",
], ids=["yes-or-no", "signature", "thanksgiving"])
async def test_no_thanks_only_counts_as_the_phrase_itself(state, body):
    status, _ = await _reply(state, body)
    assert status != "opted_out"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["No, thanks.", "STOP", "Stop.", "  unsubscribe  "],
                         ids=["comma", "stop", "stop-dot", "bare-unsubscribe"])
async def test_short_opt_out_replies_are_caught_without_the_model(state, body):
    status, prompts = await _reply(state, body)
    assert status == "opted_out" and prompts == []


@pytest.mark.asyncio
async def test_quoting_an_old_opt_out_line_is_not_an_opt_out(state):
    # Emails went out with a custom line; the line was changed afterwards.
    old = 'Reply "unsubscribe" to stop hearing from me.'
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + old
    status, _ = await _reply_after_sending(
        state, "Yes, let's talk Tuesday.\n\nOn Tue Harvey wrote:\n> " + sent.replace("\n", "\n> "),
        sent_bodies=[sent], line="Not for you? Reply no thanks.")
    assert status != "opted_out"


# Dutch Outlook: a header strip_quoted_reply doesn't know.
DUTCH_OUTLOOK = (
    "Klinkt goed, stuur me de prijzen.\n\n"
    "Van: Harvey <harvey@x.co>\nVerzonden: dinsdag 14 oktober 2026 15:02\n\n"
    "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
)


@pytest.mark.asyncio
async def test_our_quoted_email_never_reaches_the_model_even_unrecognised(state):
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
    status, prompts = await _reply_after_sending(state, DUTCH_OUTLOOK, sent_bodies=[sent])
    assert status != "opted_out"
    assert prompts and all("no thanks" not in p.lower() for p in prompts)
    assert any("Klinkt goed" in p for p in prompts)


@pytest.mark.asyncio
async def test_a_footer_cut_off_by_the_length_limit_is_not_an_opt_out(state):
    sent = "A short, plain note. Question?\n\n" + ADDRESS + "\n" + LINE
    body = ("Interested, tell me more.\n\nOn Tue Harvey wrote:\n> A short, plain note. Question?\n> "
            + ADDRESS + '\n> Not interested? Reply "no thanks"')  # truncated here
    status, _ = await _reply_after_sending(state, body, sent_bodies=[sent])
    assert status != "opted_out"


@pytest.mark.asyncio
async def test_escalation_words_in_our_own_quoted_email_dont_escalate(state):
    sent = "We help attorney offices fill their calendar. Worth a chat?"
    status, prompts = await _reply_after_sending(
        state, "Yes please.\n\nOn Tue Harvey wrote:\n> " + sent, sent_bodies=[sent])
    assert prompts  # classified by the model, not escalated by keyword


def test_inline_answers_under_a_quote_header_are_kept():
    text = strip_quoted_reply(
        "Answers inline below.\n\nOn Tue, Oct 14 Harvey wrote:\n"
        "> Would a call work?\nYes, Thursday 2pm.\n> CRM?\nHubSpot.")
    assert "Thursday 2pm" in text and "HubSpot" in text and "Would a call" not in text


def test_an_underscore_line_of_their_own_is_not_a_quote():
    assert "pricing" in strip_quoted_reply("Hi Ethan,\n____________\nSend pricing please.")


# ── Review round 1: the footer itself ──

@pytest.mark.asyncio
async def test_a_short_address_mentioned_in_the_body_still_gets_the_footer(state):
    pid = await seed_prospect(state, status="contacted")
    await _approved(state, pid, body="Saw you're in Austin, TX. Question?")
    provider = FakeProvider()
    await _sender(state, provider, address="Austin, TX")._drain_due()
    assert provider.sent[0]["body"].endswith("\n\nAustin, TX\n" + LINE)


def test_opt_out_line_must_be_recognisable_and_link_free():
    import pydantic
    for bad in ("To unsubscribe, visit https://acme.com/u", "Click here to leave",
                "Thanks for reading!"):
        with pytest.raises(pydantic.ValidationError):
            ComplianceConfig(opt_out_line=bad)
    assert ComplianceConfig(opt_out_line="Reply STOP to opt out.").opt_out_line
    assert ComplianceConfig(opt_out_line="").opt_out_line == ""
    # Left blank in YAML (null) means empty, not a crash.
    assert ComplianceConfig(postal_address=None, opt_out_line=None).opt_out_line == ""


def test_no_opt_out_line_is_warned_about_too():
    cfg = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(enabled=True)),
        compliance=ComplianceConfig(postal_address=ADDRESS, opt_out_line=""))
    assert "opt_out_line" in compliance_notice(cfg)


def test_an_edit_the_footer_would_push_over_the_limit_is_refused(client, monkeypatch, env):
    import sqlite3
    import harvey.config
    monkeypatch.setattr(harvey.config, "load_config", lambda: SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(enabled=True)),
        compliance=ComplianceConfig(postal_address=ADDRESS)))
    with sqlite3.connect(env["db"]) as db:
        db.execute("INSERT INTO outbox (id, prospect_id, campaign_id, step, kind, to_email, "
                   "subject, body, send_at, status) VALUES ('o1', 'p1', 'c1', 1, 'sequence', "
                   "'a@acme.test', 's', 'b', '2026-01-01T00:00:00', 'pending_review')")
    body = " ".join(["word"] * 215)  # fits alone, not with the footer
    r = client.post("/api/outbox/o1/edit", json={"subject": "s", "body": body})
    assert r.status_code == 400 and "too long" in r.json()["message"]


def test_campaigns_carry_the_footer_for_sending_by_hand(client, monkeypatch, env):
    import harvey.config
    monkeypatch.setattr(harvey.config, "load_config", lambda: _config(ADDRESS))
    from tests.test_dashboard_manual_send import _campaign, _prospect
    _prospect(env, "p1", "a@acme.test", status="queued")
    _campaign(env, "c1", ["p1"])
    assert client.get("/api/campaigns").json()[0]["footer"] == ADDRESS + "\n" + LINE
