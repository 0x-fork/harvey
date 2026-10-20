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
