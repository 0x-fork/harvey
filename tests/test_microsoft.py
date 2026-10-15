"""Microsoft 365 provider: token rotation, draft-then-send, threaded replies,
reading the inbox without touching it, and failing loudly."""

import json

import httpx
import pytest

from harvey.integrations import microsoft
from harvey.integrations.microsoft import GraphError, MicrosoftProvider


class Env:
    m365_client_id = "client-123"
    m365_tenant_id = "tenant-abc"


class Graph:
    """A tiny fake of the Graph + login endpoints Harvey uses."""

    def __init__(self):
        self.requests = []
        self.inbox = []
        self.fail = {}          # (method, path suffix) -> status
        self.drafts = 0

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        path = request.url.path
        method = request.method
        for (m, suffix), status in self.fail.items():
            if m == method and path.endswith(suffix):
                return httpx.Response(status, json={"error": {"code": "boom"}})
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={
                "access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-2",
            })
        if path == "/v1.0/me":
            return httpx.Response(200, json={"mail": "Owner@sender.example"})
        if path == "/v1.0/me/messages" and method == "POST":
            self.drafts += 1
            return httpx.Response(201, json={
                "id": f"draft{self.drafts}",
                "internetMessageId": f"<draft{self.drafts}@sender.example>",
                "conversationId": "conv1",
            })
        if path.endswith("/createReply"):
            self.drafts += 1
            return httpx.Response(201, json={
                "id": f"reply{self.drafts}",
                "internetMessageId": f"<reply{self.drafts}@sender.example>",
                "conversationId": "conv9",
            })
        if method == "PATCH" or method == "DELETE":
            return httpx.Response(200 if method == "PATCH" else 204, json={} if method == "PATCH" else None)
        if path.endswith("/send"):
            return httpx.Response(202)
        if path == "/v1.0/me/mailFolders/inbox/messages":
            return httpx.Response(200, json={"value": self.inbox})
        return httpx.Response(404, json={"error": {"code": "notFound"}})

    def calls(self):
        return [(r.method, r.url.path) for r in self.requests
                if r.url.host == "graph.microsoft.com"]


@pytest.fixture
def graph(tmp_path, monkeypatch):
    token_file = tmp_path / "m365_token.json"
    token_file.write_text(json.dumps({"refresh_token": "rt-1", "tenant": "tenant-abc"}))
    monkeypatch.setattr(microsoft, "TOKEN_FILE", token_file)
    return Graph()


def provider(graph):
    return MicrosoftProvider(config=None, env=Env(), transport=httpx.MockTransport(graph))


@pytest.mark.asyncio
async def test_refresh_token_rotates_and_is_saved(graph):
    p = provider(graph)
    await p._token()

    [login] = [r for r in graph.requests if r.url.host == "login.microsoftonline.com"]
    assert login.url.path == "/tenant-abc/oauth2/v2.0/token"
    form = dict(x.split("=", 1) for x in login.content.decode().split("&"))
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-1"
    assert "client_secret" not in form  # public client
    saved = json.loads(microsoft.TOKEN_FILE.read_text())
    assert saved["refresh_token"] == "rt-2"
    assert microsoft.TOKEN_FILE.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_new_email_is_drafted_then_sent_as_plain_text(graph):
    result = await provider(graph).send_email("jane@acme.com", "booking", "Hi Jane.")

    assert result.ok
    assert result.message_id == "<draft1@sender.example>"  # bounces match on this
    assert graph.calls() == [
        ("GET", "/v1.0/me"),  # which mailbox the token belongs to
        ("POST", "/v1.0/me/messages"),
        ("POST", "/v1.0/me/messages/draft1/send"),
    ]
    [draft] = [json.loads(r.content) for r in graph.requests
               if r.method == "POST" and r.url.path == "/v1.0/me/messages"]
    assert draft["body"] == {"contentType": "Text", "content": "Hi Jane."}
    assert draft["toRecipients"] == [{"emailAddress": {"address": "jane@acme.com"}}]


@pytest.mark.asyncio
async def test_reply_is_threaded_with_create_reply(graph):
    result = await provider(graph).send_email(
        "jane@acme.com", "Re: booking", "Tuesday works.",
        thread_ref="AAMk-their-message", in_reply_to="<r1@acme.com>",
    )

    assert result.ok and result.message_id == "<reply1@sender.example>"
    assert graph.calls() == [
        ("GET", "/v1.0/me"),
        ("POST", "/v1.0/me/messages/AAMk-their-message/createReply"),
        ("PATCH", "/v1.0/me/messages/reply1"),
        ("POST", "/v1.0/me/messages/reply1/send"),
    ]
    [patch] = [json.loads(r.content) for r in graph.requests if r.method == "PATCH"]
    assert patch["body"]["content"] == "Tuesday works."
    assert patch["toRecipients"][0]["emailAddress"]["address"] == "jane@acme.com"


@pytest.mark.asyncio
async def test_reply_to_a_deleted_message_is_sent_unthreaded(graph):
    graph.fail[("POST", "/createReply")] = 404
    result = await provider(graph).send_email(
        "jane@acme.com", "Re: booking", "Tuesday works.", thread_ref="gone",
    )

    assert result.ok
    assert ("POST", "/v1.0/me/messages") in graph.calls()


@pytest.mark.asyncio
async def test_failed_send_deletes_the_draft(graph):
    graph.fail[("POST", "/send")] = 500
    result = await provider(graph).send_email("jane@acme.com", "booking", "Hi.")

    assert not result.ok
    assert ("DELETE", "/v1.0/me/messages/draft1") in graph.calls()


@pytest.mark.asyncio
async def test_inbox_is_read_without_changing_it(graph):
    graph.inbox = [
        {"id": "m1", "subject": "Re: booking",
         "from": {"emailAddress": {"address": "Jane@Acme.com"}},
         "body": {"contentType": "text", "content": "Sounds good."},
         "internetMessageId": "<r1@acme.com>", "conversationId": "c1",
         "receivedDateTime": "2026-10-14T15:00:00Z",
         "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<draft1@sender.example>"}]},
        {"id": "m2", "subject": "Undeliverable: booking",
         "from": {"emailAddress": {"address": "postmaster@sender.example"}},
         "body": {"content": "couldn't be delivered"},
         "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<draft2@sender.example>"}]},
        {"id": "m3", "subject": "note to self",
         "from": {"emailAddress": {"address": "owner@sender.example"}}, "body": {"content": "x"}},
    ]
    replies = await provider(graph).get_replies()

    assert [m.provider_id for m in replies] == ["m1", "m2"]  # own mail skipped
    jane, bounce = replies
    assert jane.from_email == "jane@acme.com"
    assert jane.body == "Sounds good."
    assert jane.thread_ref == "m1"           # what createReply needs
    assert jane.in_reply_to == "<draft1@sender.example>"
    assert bounce.is_bounce and bounce.in_reply_to == "<draft2@sender.example>"

    listing = [r for r in graph.requests if r.url.path.endswith("/inbox/messages")][0]
    assert listing.headers["Prefer"] == 'outlook.body-content-type="text"'
    assert "receivedDateTime ge" in listing.url.params["$filter"]
    assert all(m in ("GET",) for m, _ in graph.calls())  # nothing marked read


@pytest.mark.asyncio
async def test_unreadable_inbox_raises_so_sends_are_held(graph):
    graph.fail[("GET", "/inbox/messages")] = 503
    with pytest.raises(GraphError):
        await provider(graph).get_replies()


@pytest.mark.asyncio
async def test_not_authorized_raises(graph):
    microsoft.TOKEN_FILE.unlink()
    p = provider(graph)
    assert not p.is_configured()
    with pytest.raises(GraphError, match="harvey microsoft auth"):
        await p.get_replies()


@pytest.mark.asyncio
async def test_handler_holds_sends_when_graph_is_down(graph, tmp_path):
    """End to end with the handler: a Graph outage is a failed read."""
    import os
    from harvey.agents.handler import Handler
    from harvey.state import StateManager
    from tests.test_outbox_native import Cfg, StubBrain

    class E(Env):
        instantly_api_key = ""

    state = StateManager(os.path.join(tmp_path, "t.db"))
    await state.init_db()
    handler = Handler(brain=StubBrain(), state=state, config=Cfg(), env=E())
    handler.provider = provider(graph)
    graph.fail[("GET", "/inbox/messages")] = 500

    assert await handler.run() is False


def test_registry_builds_the_microsoft_provider():
    from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
    from tests.test_outbox_native import Cfg

    class MsCfg(Cfg):
        class channels(Cfg.channels):
            class email(Cfg.channels.email):
                provider = "microsoft"

    assert "microsoft" in NATIVE_PROVIDERS
    assert isinstance(get_mail_provider(MsCfg(), Env()), MicrosoftProvider)


@pytest.mark.asyncio
async def test_wrong_account_is_refused(graph):
    class C:
        class persona:
            email = "you@sender.example"

    p = MicrosoftProvider(config=C(), env=Env(), transport=httpx.MockTransport(graph))
    # The fake Graph says the token belongs to owner@sender.example
    result = await p.send_email("jane@acme.com", "booking", "Hi.")
    assert not result.ok and "you@sender.example" in result.error
    assert ("POST", "/v1.0/me/messages") not in graph.calls()
    with pytest.raises(GraphError, match="signed in as owner@sender.example"):
        await p.get_replies()
    ok, detail = await p.test_connection()
    assert not ok and "persona.email" in detail


def test_pkce_challenge_matches_verifier():
    import base64
    import hashlib

    verifier, challenge = microsoft._pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.decode().rstrip("=")
