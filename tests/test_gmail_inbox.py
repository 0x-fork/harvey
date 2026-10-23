"""A partial Gmail read must not authorize the heartbeat's send phase."""
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harvey.integrations.gmail import GmailProvider


def message(message_id, sender='person@example.test', body='Hello'):
    return {'id': message_id, 'payload': {
        'mimeType': 'text/plain',
        'headers': [{'name': 'From', 'value': sender}],
        'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()},
    }}


def provider(responses):
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._request = AsyncMock(side_effect=responses)
    return p


@pytest.mark.asyncio
async def test_empty_successful_inbox():
    assert await provider([{'resultSizeEstimate': 0}]).get_replies() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('responses', [
    [None],
    [{'messages': [{'id': 'm1'}]}, None],
    [{'messages': [{'id': 'm1'}], 'nextPageToken': 'next'}, message('m1'), None],
])
async def test_incomplete_read_raises(responses):
    with pytest.raises(RuntimeError):
        await provider(responses).get_replies()


@pytest.mark.asyncio
async def test_paginates_without_changing_query_or_page_size():
    p = provider([
        {'messages': [{'id': 'm1'}], 'nextPageToken': 'opaque-token'}, message('m1'),
        {'messages': [{'id': 'm2'}]}, message('m2'),
    ])
    assert [m.provider_id for m in await p.get_replies(limit=1)] == ['m1', 'm2']
    assert p._request.call_args_list[2].kwargs['params'] == {
        'q': 'in:inbox -from:me newer_than:7d', 'maxResults': 1,
        'pageToken': 'opaque-token',
    }


@pytest.mark.asyncio
async def test_heartbeat_does_not_send_until_gmail_recovers(monkeypatch, tmp_path):
    # The shared harness routes Claude to a canned local Python response,
    # not a model/CLI service. Its mail send only appends to an in-memory list.
    from tests.test_codex_e2e import Harness, _approved_item
    harvey = Harness(monkeypatch, tmp_path, provider='claude', model='')
    await harvey.state.init_db()
    await _approved_item(harvey.state)
    gmail = provider([None, {'resultSizeEstimate': 0}])
    harvey.mail.get_replies = gmail.get_replies
    seen = {}
    def after_failure():
        seen['sent'] = list(harvey.mail.sent)
    await harvey.run_cycles(2, between={1: after_failure})
    assert seen['sent'] == []
    assert [m['to'] for m in harvey.mail.sent] == ['bob@beta.com']


@pytest.mark.asyncio
@pytest.mark.parametrize('listing', [
    {'messages': None}, {'messages': {}}, {'messages': [None]},
    {'messages': [{'id': ''}]}, {'nextPageToken': ''}, {'nextPageToken': 42},
])
async def test_malformed_page_is_not_an_empty_inbox(listing):
    with pytest.raises(RuntimeError):
        await provider([listing]).get_replies()


@pytest.mark.asyncio
async def test_repeated_page_token_fails_closed():
    p = provider([{'nextPageToken': 'same'}, {'nextPageToken': 'same'}])
    with pytest.raises(RuntimeError, match='repeated'):
        await p.get_replies()
    assert p._request.await_count == 2


@pytest.mark.asyncio
async def test_page_bound_does_not_return_a_partial_inbox(monkeypatch):
    import harvey.integrations.gmail as gmail
    monkeypatch.setattr(gmail, 'MAX_INBOX_PAGES', 2, raising=False)
    with pytest.raises(RuntimeError, match='partial'):
        await provider([{'nextPageToken': 'one'}, {'nextPageToken': 'two'}]).get_replies()


@pytest.mark.asyncio
async def test_message_repeated_across_pages_is_read_once():
    p = provider([
        {'messages': [{'id': 'm1'}], 'nextPageToken': 'next'}, message('m1'),
        {'messages': [{'id': 'm1'}, {'id': 'm2'}]}, message('m2'),
    ])
    assert [m.provider_id for m in await p.get_replies()] == ['m1', 'm2']
    assert p._request.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body', [(503, '{}'), (200, 'not json'), (200, 'null')])
async def test_http_failures_do_not_become_an_empty_inbox(monkeypatch, status, body):
    import httpx
    import harvey.integrations.gmail as gmail
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=body))
    monkeypatch.setattr(gmail.httpx, 'AsyncClient',
                        lambda **kwargs: client(transport=transport, **kwargs))
    p = GmailProvider(SimpleNamespace(), SimpleNamespace())
    p._token = AsyncMock(return_value='synthetic-token')
    with pytest.raises(RuntimeError):
        await p.get_replies()


@pytest.mark.asyncio
async def test_second_page_opt_out_cancels_approved_send(monkeypatch, tmp_path):
    from tests.test_codex_e2e import Harness, _approved_item
    harvey = Harness(monkeypatch, tmp_path, provider='claude', model='')
    await harvey.state.init_db()
    await _approved_item(harvey.state)
    gmail = provider([
        {'messages': [{'id': 'old'}], 'nextPageToken': 'next'}, message('old'),
        {'messages': [{'id': 'stop'}]}, message('stop', 'bob@beta.com', 'unsubscribe'),
    ])
    # Already handled newest mail must not hide an older opt-out forever.
    await harvey.state.mark_reply_processed('old')
    harvey.mail.get_replies = gmail.get_replies
    await harvey.run_cycles(1)
    assert harvey.mail.sent == []
    assert await harvey.state.is_reply_processed('stop')
    assert len(await harvey.state.get_outbox(status='cancelled')) == 1
