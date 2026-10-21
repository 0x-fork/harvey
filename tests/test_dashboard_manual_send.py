"""Sending by hand from the dashboard.

Covers the Campaigns tab's "Open in Gmail" data (recipients per campaign) and
the "Mark as sent" toggle (never erases a reply, refuses unknown prospects).
"""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.state import StateManager

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    asyncio.run(StateManager(db_path=str(db)).init_db())
    return {"db": db, "token": dash.token_store.get()}


@pytest.fixture
def client(env):
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {env['token']}"}) as c:
        yield c


def _sql(env, query, params=()):
    with sqlite3.connect(env["db"]) as db:
        db.execute(query, params)


def _prospect(env, pid, email, email_status="verified", status="new"):
    _sql(env, "INSERT INTO prospects (id, first_name, last_name, email, email_status, "
              "status, company) VALUES (?, '', '', ?, ?, ?, 'Acme')",
         (pid, email, email_status, status))


def _campaign(env, cid, prospect_ids):
    _sql(env, "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, "
              "status) VALUES (?, ?, 'email', ?, ?, 'draft')",
         (cid, cid, json.dumps([{"step": 1, "subject": "hi", "body": "Hi {{company}}",
                                 "delay_days": 0}]), json.dumps(prospect_ids)))


# ── Campaign recipients ──


def test_campaigns_list_their_recipients(client, env):
    _prospect(env, "p1", "a@acme.test")
    _prospect(env, "p2", "")  # no address: nothing to open in Gmail
    _campaign(env, "c1", ["p1", "p2"])
    rows = client.get("/api/campaigns").json()
    assert [r["email"] for r in rows[0]["recipients"]] == ["a@acme.test"]
    assert rows[0]["recipients"][0]["status"] == "new"


def test_campaign_with_no_prospects_has_no_recipients(client, env):
    _campaign(env, "c1", [])
    assert client.get("/api/campaigns").json()[0]["recipients"] == []


# ── Mark as sent ──


def test_mark_sent_and_undo(client, env):
    _prospect(env, "p1", "a@acme.test")
    assert client.post("/api/prospects/p1/sent", json={"sent": True}).json()["sent"] is True
    assert client.get("/api/campaigns").json() == []  # no campaign needed to toggle
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT status FROM prospects").fetchone()[0] == "contacted"
        assert db.execute("SELECT action_type FROM actions").fetchone()[0] == "manual_send"
    client.post("/api/prospects/p1/sent", json={"sent": False})
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT status FROM prospects").fetchone()[0] == "new"


def test_unticking_never_erases_a_reply(client, env):
    _prospect(env, "p1", "a@acme.test", status="replied")
    r = client.post("/api/prospects/p1/sent", json={"sent": False}).json()
    assert r == {"success": True, "sent": True, "status": "replied"}
    with sqlite3.connect(env["db"]) as db:
        assert db.execute("SELECT status FROM prospects").fetchone()[0] == "replied"


def test_unknown_prospect_is_refused(client):
    assert client.post("/api/prospects/nope/sent", json={"sent": True}).status_code == 404


def test_missing_body_means_sent(client, env):
    _prospect(env, "p1", "a@acme.test")
    assert client.post("/api/prospects/p1/sent").json()["sent"] is True


# ── Never email someone twice ──


def _outbox(env, oid, pid, status, step=1, campaign="c1"):
    _sql(env, "INSERT INTO outbox (id, prospect_id, campaign_id, step, to_email, subject, body, "
              "send_at, status) VALUES (?, ?, ?, ?, 'a@acme.test', 's', 'b', "
              "'2026-01-01T00:00:00', ?)", (oid, pid, campaign, step, status))


def _status(env, table, row_id):
    with sqlite3.connect(env["db"]) as db:
        return db.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


def test_sending_by_hand_cancels_what_harvey_had_queued(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _outbox(env, "o1", "p1", "approved", step=1)
    _outbox(env, "o2", "p1", "pending_review", step=2)
    client.post("/api/prospects/p1/sent", json={"sent": True})
    assert _status(env, "outbox", "o1") == "cancelled"
    assert _status(env, "outbox", "o2") == "cancelled"


def test_undo_puts_the_prospect_and_their_drafts_back_for_review(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _outbox(env, "o1", "p1", "approved")
    client.post("/api/prospects/p1/sent", json={"sent": True})
    assert client.post("/api/prospects/p1/sent", json={"sent": False}).json()["sent"] is False
    assert _status(env, "prospects", "p1") == "queued"   # not 'new': the Writer won't redraft
    assert _status(env, "outbox", "o1") == "pending_review"  # back to you, never straight out


def test_undo_never_cancels_an_unrelated_cancellation(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _outbox(env, "o1", "p1", "cancelled")
    client.post("/api/prospects/p1/sent", json={"sent": True})
    client.post("/api/prospects/p1/sent", json={"sent": False})
    assert _status(env, "outbox", "o1") == "cancelled"


def test_unticking_someone_harvey_emailed_is_refused(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _outbox(env, "o1", "p1", "sent")
    r = client.post("/api/prospects/p1/sent", json={"sent": False})
    assert r.status_code == 409
    assert _status(env, "prospects", "p1") == "contacted"


def test_unticking_someone_sent_through_instantly_is_refused(client, env):
    _prospect(env, "p1", "a@acme.test", status="contacted")
    _campaign(env, "c1", ["p1"])
    _sql(env, "UPDATE campaigns SET instantly_campaign_id = 'inst-1', status = 'active'")
    assert client.post("/api/prospects/p1/sent", json={"sent": False}).status_code == 409
    assert _status(env, "prospects", "p1") == "contacted"


def test_recipients_say_when_harvey_is_sending_to_them(client, env):
    _prospect(env, "p1", "a@acme.test", status="queued")
    _prospect(env, "p2", "b@acme.test", status="queued")
    _campaign(env, "c1", ["p1", "p2"])
    _outbox(env, "o1", "p1", "pending_review")
    recips = {r["id"]: r for r in client.get("/api/campaigns").json()[0]["recipients"]}
    assert recips["p1"]["harvey_sending"] is True
    assert recips["p2"]["harvey_sending"] is False


@pytest.mark.parametrize("payload", [[], "x"])
def test_a_body_that_is_not_an_object_is_refused(client, env, payload):
    _prospect(env, "p1", "a@acme.test")
    assert client.post("/api/prospects/p1/sent", json=payload).status_code == 400
    assert _status(env, "prospects", "p1") == "new"
