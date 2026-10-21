"""Warmup: the cold-email ramp for a new domain, and `harvey warmup`'s
read-only deliverability checks.

Based on community PR #18 by Tariz Shahid.
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dns.exception
import pytest

from harvey import deliverability as d
from harvey.config import EmailChannelConfig, WarmupConfig
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)


# ── The ramp ──

def test_no_start_date_means_no_ramp():
    assert WarmupConfig().daily_cap(30) == 30
    assert EmailChannelConfig(max_daily_sends=12).cold_daily_cap() == 12


def test_ramp_holds_then_climbs_to_ceiling():
    w = WarmupConfig(start_date="2026-10-01", min_days=14, start_sends=5, daily_increase=3)
    cap = lambda day: w.daily_cap(30, date(2026, 10, day))  # noqa: E731
    assert [cap(1), cap(14)] == [0, 0]          # warming only
    assert [cap(15), cap(16), cap(20)] == [5, 8, 20]
    assert cap(31) == 30                         # never above max_daily_sends


def test_bad_or_blank_settings():
    with pytest.raises(ValueError):
        WarmupConfig(start_date="10/01/2026")
    with pytest.raises(ValueError):
        WarmupConfig(min_days=-1)
    assert WarmupConfig(start_date=None).start_date == ""  # left blank in YAML


def _now_iso(**delta):
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(**delta)).isoformat()


async def _approved(state, pid, kind, step=1, campaign="c1"):
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.com",
        subject="hello there", body="A short, plain note. Question?", send_at=_now_iso(),
        status="approved", campaign_id=campaign if kind == "sequence" else "", step=step, kind=kind)


def _ramped_sender(state, provider, days_in, **ramp):
    sender = make_sender(state, provider)
    start = (date.today() - timedelta(days=days_in)).isoformat()
    sender.config.channels.email.warmup = WarmupConfig(start_date=start, **ramp)
    return sender


@pytest.mark.asyncio
async def test_while_warming_cold_email_waits_but_replies_go(state):
    cold = await seed_prospect(state, email="jane@acme.com", status="queued")
    await _approved(state, cold, "sequence")
    warm = await seed_prospect(state, email="bob@acme.com", status="replied")
    await state.add_outbox_item(
        prospect_id=warm, to_email="bob@acme.com", subject="Re: hi",
        body="Sure, Tuesday works.", send_at=_now_iso(), status="approved", kind="reply")
    provider = FakeProvider()
    try:
        await _ramped_sender(state, provider, days_in=3, min_days=14)._drain_due()
    finally:
        make_sender(state, provider).config.channels.email.warmup = WarmupConfig()
    assert [m["to"] for m in provider.sent] == ["bob@acme.com"]
    assert len(await state.get_outbox(status="approved")) == 1  # the cold one waits


@pytest.mark.asyncio
async def test_after_warming_cold_email_is_capped_at_todays_ramp(state):
    for n in range(4):
        pid = await seed_prospect(state, email=f"p{n}@acme.com", status="queued")
        await state.add_outbox_item(
            prospect_id=pid, to_email=f"p{n}@acme.com", subject="hello there",
            body="A short, plain note. Question?", send_at=_now_iso(), status="approved",
            campaign_id="c1", step=1)
    provider = FakeProvider()
    try:
        # Day 15 of a 14-day warmup: start_sends + one day's increase = 2.
        await _ramped_sender(state, provider, days_in=15, min_days=14, start_sends=1,
                             daily_increase=1)._drain_due()
    finally:
        make_sender(state, provider).config.channels.email.warmup = WarmupConfig()
    assert len(provider.sent) == 2


@pytest.mark.asyncio
async def test_instantly_adds_no_more_leads_than_todays_ramp(state, monkeypatch):
    from harvey.agents.sender import Sender
    from harvey.config import EnvConfig
    from harvey.models.campaign import Campaign, EmailStep
    ids = [await seed_prospect(state, email=f"p{n}@acme.com", status="queued") for n in range(6)]
    warm_start = (date.today() - timedelta(days=14)).isoformat()
    config = SimpleNamespace(channels=SimpleNamespace(email=EmailChannelConfig(
        provider="instantly", require_approval=False, max_daily_sends=50,
        warmup=WarmupConfig(start_date=warm_start, min_days=14, start_sends=3))))
    sender = Sender(None, state, config, EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.set_campaign_emails.return_value = {}
    sender.instantly.add_leads.return_value = {}
    await sender._deploy_campaign(Campaign(
        id="c", name="Draft", instantly_campaign_id="existing", prospect_ids=ids,
        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")]))
    leads = sender.instantly.add_leads.await_args.args[1]
    assert len(leads) == 3


# ── The checks (DNS stubbed) ──

def _txt(records):
    return lambda name: records.get(name, [])


def test_spf_checks(monkeypatch):
    monkeypatch.setattr(d, "_txt", _txt({"a.com": ["v=spf1 include:_spf.google.com ~all"]}))
    assert d.check_spf("a.com").status == d.OK
    monkeypatch.setattr(d, "_txt", _txt({"a.com": ["v=spf1 +all"]}))
    assert d.check_spf("a.com").status == d.FAIL
    monkeypatch.setattr(d, "_txt", _txt({"a.com": ["v=spf1 -all", "v=spf1 ~all"]}))
    assert d.check_spf("a.com").status == d.FAIL
    monkeypatch.setattr(d, "_txt", _txt({}))
    assert d.check_spf("a.com").status == d.FAIL


def test_dmarc_and_dkim(monkeypatch):
    monkeypatch.setattr(d, "_txt", _txt({
        "_dmarc.a.com": ["v=DMARC1; p=none; rua=mailto:x@a.com"],
        "google._domainkey.a.com": ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
        "s1._domainkey.b.com": ["v=DKIM1; p="],  # revoked key
    }))
    assert d.check_dmarc("a.com").status == d.WARN
    assert d.check_dmarc("b.com").status == d.FAIL
    assert d.check_dkim("a.com").status == d.OK
    assert d.check_dkim("b.com", "s1").status == d.FAIL


def test_blocklist_refusal_is_not_a_listing(monkeypatch):
    answers = {"a.com.dbl.spamhaus.org": ["127.255.255.254"],
               "a.com.multi.surbl.org": ["127.0.0.2"]}
    monkeypatch.setattr(d, "_a", lambda name: answers.get(name, []))
    by_zone = {c.name.split()[-1]: c.status for c in d.check_domain_blocklists("a.com")}
    assert by_zone == {"dbl.spamhaus.org": d.INFO, "multi.surbl.org": d.FAIL,
                       "multi.uribl.com": d.OK}


def test_a_dns_timeout_is_unknown_not_missing(monkeypatch):
    def timeout(*a, **k):
        raise dns.exception.Timeout()
    monkeypatch.setattr(d.dns.resolver, "resolve", timeout)
    checks = [d.check_mx("a.com"), d.check_spf("a.com"), d.check_dkim("a.com"),
              d.check_dmarc("a.com"), *d.check_domain_blocklists("a.com"),
              *d.check_ip_blocklists("smtp.a.com")]
    assert {c.status for c in checks} == {d.INFO}


def test_warmup_check_reports_progress():
    w = WarmupConfig(start_date="2026-10-01", min_days=14)
    assert d.check_warmup(w, 30, date(2026, 10, 5)).status == d.FAIL
    assert d.check_warmup(w, 30, date(2026, 10, 20)).status == d.OK
    assert d.check_warmup(WarmupConfig(), 30).status == d.WARN


@pytest.mark.parametrize("domain, ok", [
    ("acme.com", True), ("mail.acme.co.uk", True),
    ("acme", False), ("acme.com/../x", False), ("-acme.com", False), ("", False),
])
def test_only_real_domain_names_are_checked(domain, ok):
    assert bool(d.DOMAIN_RE.match(domain)) is ok
