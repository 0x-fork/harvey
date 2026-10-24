"""Addresses and domains on `compliance.do_not_contact` never get an email.

Competitors, existing customers, anyone under a legal hold. The check lives in
the gate rather than in discovery so it holds at the last moment before a send,
including for an email a human already approved in the Outbox.
"""

from types import SimpleNamespace

import pytest

from harvey.config import ComplianceConfig
from harvey.gate import do_not_contact_match, pre_send_check
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_sender, seed_prospect, state,
)

BODY = "A short, plain note. Question?"


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _prospect(email):
    return SimpleNamespace(email=email, email_status="verified")


def _check(to_email, entries, kind="sequence"):
    return pre_send_check(
        to_email, "hello there", BODY, prospect=_prospect(to_email),
        kind=kind, do_not_contact=entries,
    )


# ── Matching ──

def test_a_domain_blocks_every_address_at_it():
    assert do_not_contact_match("jane@acme.example", ["acme.example"]) == "acme.example"
    assert do_not_contact_match("rob@acme.example", ["acme.example"]) == "acme.example"


def test_a_domain_covers_its_subdomains():
    assert do_not_contact_match("jane@mail.acme.example", ["acme.example"]) == "acme.example"


def test_a_lookalike_domain_is_not_blocked():
    """The boundary that stops a blocklist overreaching: a plain suffix test
    would block this, and it is a different company."""
    assert do_not_contact_match("jane@notacme.example", ["acme.example"]) == ""
    assert do_not_contact_match("jane@acme.example.evil.test", ["acme.example"]) == ""


def test_an_entry_with_an_at_blocks_only_that_address():
    entries = ["jane@acme.example"]
    assert do_not_contact_match("jane@acme.example", entries) == "jane@acme.example"
    assert do_not_contact_match("rob@acme.example", entries) == ""


def test_an_empty_list_blocks_nothing():
    assert do_not_contact_match("jane@acme.example", []) == ""
    assert do_not_contact_match("jane@acme.example", ()) == ""


def test_entries_are_normalised_by_the_config():
    cfg = ComplianceConfig(do_not_contact=[" @Acme.EXAMPLE ", "www.other.example", "", "acme.example"])
    # lowercased, @ and www. stripped, blanks and duplicates dropped
    assert cfg.do_not_contact == ["acme.example", "other.example"]


def test_a_single_string_is_accepted_as_a_list_of_one():
    assert ComplianceConfig(do_not_contact="acme.example").do_not_contact == ["acme.example"]


def test_a_hand_built_tuple_is_normalised_too():
    """Callers outside the config should behave the same way."""
    assert do_not_contact_match("jane@acme.example", ("  ACME.Example ",)) == "acme.example"


# ── The gate ──

def test_the_gate_blocks_a_listed_recipient():
    result = _check("jane@acme.example", ["acme.example"])
    assert not result
    assert any("do-not-contact" in r for r in result.reasons)


def test_the_gate_passes_an_unlisted_recipient():
    assert _check("jane@other.example", ["acme.example"])


def test_replies_are_blocked_too():
    """A reply is still contact. The operator can answer by hand."""
    assert not _check("jane@acme.example", ["acme.example"], kind="reply")


def test_nothing_changes_when_the_list_is_empty():
    assert _check("jane@acme.example", [])


# ── End to end ──

@pytest.mark.asyncio
async def test_an_approved_email_to_a_listed_domain_never_leaves(state):
    """The point of putting this in the gate: approval does not override it."""
    pid = await seed_prospect(state, email="jane@acme.example")
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="jane@acme.example", subject="hello there",
        body=BODY, send_at=_now_iso(), status="approved", kind="reply",
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(do_not_contact=["acme.example"])

    await sender._drain_due()

    assert provider.sent == [], "an email went to a do-not-contact domain"
    item = await state.get_outbox_item(item_id)
    assert item["status"] == "failed"
    assert "do-not-contact" in item["error"]


@pytest.mark.asyncio
async def test_an_unlisted_recipient_still_sends(state):
    """The guard must not cost everyone else their mail."""
    pid = await seed_prospect(state, email="jane@other.example")
    await state.add_outbox_item(
        prospect_id=pid, to_email="jane@other.example", subject="hello there",
        body=BODY, send_at=_now_iso(), status="approved", kind="reply",
    )
    provider = FakeProvider()
    sender = make_sender(state, provider)
    sender.config.compliance = ComplianceConfig(do_not_contact=["acme.example"])

    await sender._drain_due()

    assert len(provider.sent) == 1
