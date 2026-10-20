"""Company-inbox fallback: a business that names nobody is reached at its published inbox."""

from types import SimpleNamespace

import pytest

import harvey.agents.scout as scout_mod
import harvey.integrations.email_finder as ef
from harvey.agents.scout import Scout
from harvey.agents.sender import Sender
from harvey.models.prospect import COMPANY_INBOX_SOURCE, Prospect


def inbox(**kw):
    base = dict(email="info@acme.com", company="Acme Roofing", title="General inbox",
                source=COMPANY_INBOX_SOURCE)
    return Prospect(**{**base, **kw})


# ── model ──

def test_inbox_prospect_is_valid_without_a_name():
    p = inbox()
    assert p.is_company_inbox and p.is_valid()
    assert p.full_name() == "Acme Roofing team"


def test_inbox_needs_an_address_and_named_people_still_need_names():
    assert not inbox(email="").is_valid()
    assert not Prospect(email="a@b.com", title="Owner").is_valid()


def test_render_greets_an_inbox_as_there():
    sender = Sender.__new__(Sender)
    assert sender._render("Hi {{first_name}}, {{company}}", inbox()) == "Hi there, Acme Roofing"
    named = Prospect(first_name="Jo", last_name="Li", title="Owner", company="Acme")
    assert sender._render("Hi {{first_name}}", named) == "Hi Jo"


# ── verification ──

@pytest.mark.parametrize("verdict, expected", [
    ({"status": "safe"}, "verified"),
    ({"status": "valid"}, "verified"),
    ({"status": "spamtrap"}, "invalid"),
    ({"status": "disposable"}, "invalid"),
    ({"status": "unknown"}, None),
    ({"status": "safe", "catch_all": True}, "risky"),
])
def test_reoon_verdicts(verdict, expected):
    assert ef._status_from_verdict(verdict) == expected


@pytest.mark.parametrize("graded, expected", [
    ("verified", "verified"),
    ("risky", "risky"),        # catch-all stays risky: send_to_risky decides
    ("invalid", "invalid"),
    (None, "guess"),           # inconclusive stays a guess: never sent
])
@pytest.mark.asyncio
async def test_published_address_grading(monkeypatch, graded, expected):
    async def mx(domain):
        return "smtp.google.com"

    async def check(email, mx_type, env):
        return graded, graded == "risky"

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "_verify_candidate", check)
    assert await ef.verify_published_address("info@acme.com") == expected


@pytest.mark.asyncio
async def test_published_address_without_mail_server_is_invalid(monkeypatch):
    async def no_mx(domain):
        return None

    monkeypatch.setattr(ef, "get_mx_host", no_mx)
    assert await ef.verify_published_address("info@acme.com") == "invalid"


# ── scout ──

PAGES = {
    "https://acme.com": '<a href="mailto:careers@acme.com">Jobs</a> <a href="mailto:info@acme.com">Email us</a>',
    "https://acme.com/contact": "Write to office@acme.com or partner@other.com",
}


def make_scout(pages, verdict="verified"):
    scout = Scout.__new__(Scout)
    scout.env = None
    scout._seen_prospect_keys = set()

    async def resolve(domain, retries=1):
        home = pages.get("https://acme.com")
        return ("https://acme.com", SimpleNamespace(text=home)) if home is not None else ("", None)

    async def fetch(url, **kw):
        text = pages.get(url)
        return SimpleNamespace(text=text) if text is not None else None

    async def not_dup(prospect):
        return False

    scout._resolve_base_url = resolve
    scout._fetch = fetch
    scout._is_duplicate_prospect = not_dup
    return scout


@pytest.mark.asyncio
async def test_finds_the_most_owner_like_on_domain_address():
    assert await make_scout(PAGES)._find_inbox_address("acme.com") == "office@acme.com"


@pytest.mark.asyncio
async def test_no_usable_address_means_no_inbox():
    pages = {"https://acme.com": '<a href="mailto:careers@acme.com">x</a> noreply@acme.com'}
    assert await make_scout(pages)._find_inbox_address("acme.com") == ""


@pytest.mark.asyncio
async def test_inbox_prospect_carries_its_verification(monkeypatch):
    async def graded(email, env=None):
        return "verified"

    monkeypatch.setattr(scout_mod, "verify_published_address", graded)
    p = await make_scout(PAGES)._company_inbox_prospect("c1", "acme.com", "Acme Roofing", "Roofing")
    assert p.email == "office@acme.com" and p.email_status == "verified"
    assert p.is_company_inbox and p.company_id == "c1"


# ── fallback only when the company has nobody ──

def fallback_scout(team, existing=(), duplicate=False):
    """A Scout whose team page, email resolution and DB are stand-ins."""
    scout = Scout.__new__(Scout)
    scout._seen_prospect_keys = set()
    scout.inbox_calls = 0

    async def scrape(domain):
        return team

    async def resolve(first, last, domain):
        return f"{first.lower()}@acme.com", "verified"

    async def is_dup(prospect):
        return duplicate

    async def existing_contacts(company_id):
        return list(existing)

    async def inbox_prospect(*args, **kw):
        scout.inbox_calls += 1
        return inbox()

    scout._scrape_team_page = scrape
    scout._title_matches_icp = lambda title: True
    scout._resolve_email = resolve
    scout._infer_seniority = lambda title: ""
    scout._is_duplicate_prospect = is_dup
    scout._company_inbox_prospect = inbox_prospect
    scout.state = SimpleNamespace(get_contacts_for_company=existing_contacts)
    return scout


OWNER = [{"first_name": "Jo", "last_name": "Li", "title": "Owner"}]


@pytest.mark.asyncio
async def test_inbox_used_when_the_site_names_nobody():
    scout = fallback_scout(team=[])
    contacts = await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing")
    assert [p.is_company_inbox for p in contacts] == [True]


@pytest.mark.asyncio
async def test_no_inbox_when_the_named_owner_is_already_a_prospect():
    # A revisit: the owner is skipped as a duplicate, but the company must
    # not be emailed a second time through info@.
    scout = fallback_scout(team=OWNER, duplicate=True)
    assert await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing") == []
    assert scout.inbox_calls == 0


@pytest.mark.asyncio
async def test_no_inbox_when_the_company_already_has_a_contact():
    # Someone at the company came from another source (LinkedIn, a job board).
    known = Prospect(first_name="Jo", last_name="Li", title="Owner", email="jo@acme.com")
    scout = fallback_scout(team=[], existing=[known])
    assert await scout._contacts_from_company("c1", "acme.com", "Acme", "Roofing") == []
    assert scout.inbox_calls == 0


@pytest.mark.asyncio
async def test_no_inbox_without_a_company_record():
    scout = fallback_scout(team=[])
    assert await scout._contacts_from_company("", "acme.com", "Acme", "Roofing") == []
    assert scout.inbox_calls == 0


# ── writer ──

def test_inbox_prospects_get_their_own_campaign():
    from harvey.agents.writer import Writer
    writer = Writer.__new__(Writer)
    writer.config = SimpleNamespace(channels=SimpleNamespace(email=SimpleNamespace(provider="gmail")))
    named = Prospect(first_name="Jo", last_name="Li", title="Owner", industry="Roofing", email="jo@a.com")
    batches = writer._group_prospects([named, inbox(industry="Roofing")])
    assert set(batches) == {"Roofing-outreach", "Roofing-inbox-outreach"}
