"""The Outbox review desk: the facts behind each draft, and editing it.

Reviewing a cold email means checking its claims against what Harvey actually
found. The desk shows that next to the draft, and lets a reviewer fix a draft
that is nearly right instead of throwing the lead away.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.models import Company
from harvey.models.campaign import Campaign
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


async def _seed(db_path, status="pending_review"):
    state = StateManager(str(db_path))
    await state.init_db()
    await seed_signal_catalog(state)
    company_id = await state.add_company(Company(
        name="Proven Resource", domain="proven.lovable.app",
        website="https://proven.lovable.app", description="Client portal for law firms",
        industry='site:lovable.app "client portal"'))
    await state.add_observations([
        {"company_id": company_id, "signal_code": "TECH_STACK", "value_text": "Lovable, Stripe"},
        {"company_id": company_id, "signal_code": "RUNNING_GOOGLE_ADS", "value_num": 1},
    ])
    prospect_id = await state.add_prospect(Prospect(
        email="team@provenresource.com", email_status="risky", title="General inbox",
        source="scraped_mailto", company="Proven Resource", company_id=company_id,
        score=45, personalization_notes="Lovable-built client portal."))
    campaign_id = await state.add_campaign(Campaign(
        id="", name='site:lovable.app "client portal"-outreach', channel="email",
        sequence=[], prospect_ids=[prospect_id], status="active"))
    item_id = await state.add_outbox_item(
        prospect_id=prospect_id, to_email="team@provenresource.com",
        subject="your client portal", body="Hello welcome team,\n\nHaris",
        send_at="2026-01-01T00:00:00", status=status, campaign_id=campaign_id)
    return state, item_id


def test_pending_drafts_carry_the_facts_behind_them(client):
    asyncio.run(_seed(client.db_path))
    item = client.get("/api/outbox").json()["pending"][0]
    ctx = item["context"]
    assert ctx["company"] == "Proven Resource"
    assert ctx["website"] == "https://proven.lovable.app"
    assert ctx["description"] == "Client portal for law firms"
    assert ctx["campaign"] == 'site:lovable.app "client portal"-outreach'
    assert ctx["found_by"] == 'site:lovable.app "client portal"'
    assert ctx["contact_source"] == "scraped_mailto"
    assert ctx["email_status"] == "risky"
    assert ctx["notes"] == "Lovable-built client portal."
    assert ctx["signals"] == {"Website platform / tools": "Lovable, Stripe", "Running Google Ads": "yes"}


def test_editing_a_draft_changes_it_but_does_not_approve_it(client):
    state, item_id = asyncio.run(_seed(client.db_path))
    r = client.post(f"/api/outbox/{item_id}/edit", json={
        "subject": "your portal", "body": "Hello Proven Resource team,\n\nHaris"})
    assert r.json() == {"success": True}
    item = asyncio.run(state.get_outbox_item(item_id))
    assert item["subject"] == "your portal"
    assert item["body"] == "Hello Proven Resource team,\n\nHaris"
    assert item["status"] == "pending_review"


@pytest.mark.parametrize("status", ["approved", "sent", "rejected"])
def test_only_drafts_awaiting_review_can_be_edited(client, status):
    state, item_id = asyncio.run(_seed(client.db_path, status=status))
    r = client.post(f"/api/outbox/{item_id}/edit", json={"subject": "x", "body": "y"})
    assert r.status_code == 409
    assert asyncio.run(state.get_outbox_item(item_id))["subject"] == "your client portal"


def test_edit_needs_both_subject_and_body(client):
    _, item_id = asyncio.run(_seed(client.db_path))
    assert client.post(f"/api/outbox/{item_id}/edit",
                       json={"subject": "  ", "body": "text"}).status_code == 400
    assert client.post(f"/api/outbox/{item_id}/edit",
                       json={"subject": "s"}).status_code == 400
