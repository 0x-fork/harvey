"""When Harvey holds sending because replies couldn't be read, you see it.

Sending waits for replies so an opt-out stops its follow-up (main.py). That
used to show only as a log line, so sending could stall for days unnoticed:
a Gmail sign-in that lapsed, an inbox over the read limit. Now the hold is
recorded with why and since when, and Today and `harvey status` say so.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from harvey.sending_mode import held_message
from harvey.state import SENDS_HELD_KEY, StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    await s.init_db()
    return s


# ── the handler says why it couldn't read ──


@pytest.mark.asyncio
async def test_the_handler_keeps_the_reason_a_read_failed(state):
    from tests.test_outbox_native import FakeProvider, make_handler
    provider = FakeProvider()
    provider.get_replies = AsyncMock(side_effect=RuntimeError("not signed in to Gmail"))
    handler = make_handler(state, provider)
    assert await handler._run_native() is False
    assert "not signed in to Gmail" in handler.last_read_problem
    provider.get_replies = AsyncMock(return_value=[])
    assert await handler._run_native() is True
    assert handler.last_read_problem == ""


@pytest.mark.asyncio
async def test_gmail_without_a_sign_in_says_so():
    from harvey.integrations.gmail import GmailError, GmailProvider
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value=None)
    with pytest.raises(GmailError, match="harvey gmail auth"):
        await p.get_replies()


# ── the hold is recorded, kept and cleared ──


@pytest.mark.asyncio
async def test_a_hold_keeps_its_start_and_clears_when_sending_runs(state):
    await state.note_sends_held("Gmail: not signed in")
    first = await state.current_sends_held()
    await state.note_sends_held("Gmail: not signed in")
    again = await state.current_sends_held()
    assert again["since"] == first["since"] and again["reason"] == "Gmail: not signed in"
    await state.clear_sends_held()
    assert await state.current_sends_held() is None


@pytest.mark.asyncio
async def test_a_hold_nobody_has_seen_for_half_a_day_is_not_shown(state):
    old = (datetime.now(timezone.utc) - timedelta(hours=13)).replace(tzinfo=None).isoformat()
    await state.set_setting(SENDS_HELD_KEY, json.dumps({"since": old, "last_seen": old, "reason": "x"}))
    assert await state.current_sends_held() is None        # Harvey stopped: no stale card


@pytest.mark.asyncio
async def test_the_heartbeat_records_and_clears_it(state):
    from harvey.main import record_send_phase
    await record_send_phase(state, "replies couldn't all be read: Gmail: not signed in")
    assert "not signed in" in (await state.current_sends_held())["reason"]
    await record_send_phase(state, "")
    assert await state.current_sends_held() is None


# ── the wording ──


@pytest.mark.parametrize("reason,fix", [
    ("Gmail: not signed in to Gmail. Run 'harvey gmail auth'.", "harvey gmail auth"),
    ("Microsoft: not authorized. Run 'harvey microsoft auth'.", "harvey microsoft auth"),
    ("inbox has more than 40 pages; refusing a partial read", "more mail"),
    ("Graph 503 on GET /me/messages", "tries again every cycle"),
])
def test_the_message_names_the_fix(reason, fix):
    title, detail = held_message({"reason": reason, "since": "2026-10-24T08:00:00"}, waiting=3)
    assert "isn't sending" in title
    assert fix in detail and "3 emails are waiting" in detail and "opt-out" in detail


# ── where it shows ──


def test_today_shows_the_hold_first(client):
    st = StateManager(str(client.db_path))
    asyncio.run(st.init_db())
    asyncio.run(st.note_sends_held("Gmail: not signed in to Gmail. Run 'harvey gmail auth'."))
    items = client.get("/api/today").json()["items"]
    held = [i for i in items if i["key"] == "sends-held"]
    assert held and held[0]["tone"] == "bad" and "harvey gmail auth" in held[0]["detail"]
    asyncio.run(st.clear_sends_held())
    assert not [i for i in client.get("/api/today").json()["items"] if i["key"] == "sends-held"]


def test_harvey_status_shows_it(tmp_path, monkeypatch, capsys):
    import argparse
    import harvey.state as state_mod
    from harvey import cli
    db = tmp_path / "s.db"
    st = StateManager(str(db))
    asyncio.run(st.init_db())
    asyncio.run(st.note_sends_held("Gmail: not signed in to Gmail. Run 'harvey gmail auth'."))
    real = state_mod.StateManager
    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **k: real(str(db)))
    cli.cmd_status(argparse.Namespace(check=False))
    out = capsys.readouterr().out
    assert "NOT SENDING" in out and "harvey gmail auth" in out
