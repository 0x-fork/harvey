"""/api/conversations carries the whole exchange: the cold emails a reply
answers live in the Outbox, not in the stored thread."""

import asyncio
import json
import sqlite3

from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from harvey.state import StateManager


def _sql(db, query, params=()):
    with sqlite3.connect(db) as conn:
        conn.execute(query, params)
    conn.close()


def _seed(db):
    asyncio.run(StateManager(str(db)).init_db())
    _sql(db, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p1', 'Iris', 'i@x.test', 'replied')")
    _sql(db, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p2', 'Ben', 'b@x.test', 'contacted')")
    thread = [{"sender": "prospect", "content": "What does it cost?", "timestamp": "2026-10-18T10:00:00"}]
    _sql(db, "INSERT INTO conversations (id, prospect_id, campaign_id, thread_json, intent, status, updated_at) "
             "VALUES ('c1', 'p1', 'k1', ?, 'question', 'open', '2026-10-18T10:00:00')", (json.dumps(thread),))
    rows = [
        ("o1", "p1", "sequence", 1, "sent", "quick question", "Hi Iris, one.", "2026-10-10T09:00:00", ""),
        ("o2", "p1", "sequence", 2, "sent", "re: booking", "Hi Iris, two.", "2026-10-13T09:00:00", ""),
        ("o3", "p1", "sequence", 3, "cancelled", "closing", "Hi Iris, three.", None, ""),
        ("o4", "p1", "reply", 1, "pending_review", "Re: re: booking", "It's $49 a month.", None, "c1"),
        ("o5", "p1", "reply", 1, "sent", "Re: earlier", "Already in the thread.", "2026-10-18T11:00:00", "c1"),
        ("o6", "p2", "sequence", 1, "sent", "hello Ben", "Hi Ben.", "2026-10-11T09:00:00", ""),
    ]
    for oid, pid, kind, step, status, subject, body, sent_at, conv in rows:
        _sql(db, "INSERT INTO outbox (id, prospect_id, campaign_id, conversation_id, kind, step, to_email, "
                 "subject, body, status, sent_at, send_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
             (oid, pid, "k1" if kind == "sequence" else "", conv, kind, step, "x@x.test", subject, body,
              status, sent_at, "2026-10-19T09:00:00"))


def test_conversations_include_the_emails_they_answer_and_a_waiting_draft(client):
    _seed(client.db_path)
    [convo] = client.get("/api/conversations").json()
    assert [(e["step"], e["subject"]) for e in convo["emails"]] == [(1, "quick question"), (2, "re: booking")]
    assert all("sent_at" in e and "body" in e for e in convo["emails"])   # cancelled step 3 left out
    assert convo["draft"]["id"] == "o4" and convo["draft"]["body"] == "It's $49 a month."
    assert convo["thread"][0]["content"] == "What does it cost?"


def test_a_conversation_without_emails_or_draft(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    _sql(client.db_path, "INSERT INTO prospects (id, first_name, email, status) VALUES ('p9', 'Ana', 'a@x.test', 'replied')")
    _sql(client.db_path, "INSERT INTO conversations (id, prospect_id, thread_json, status) VALUES ('c9', 'p9', '[]', 'open')")
    [convo] = client.get("/api/conversations").json()
    assert convo["emails"] == [] and convo["draft"] is None
