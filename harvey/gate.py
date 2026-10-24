"""Deterministic pre-send gate — the last line before an email leaves.

LLM instructions are a floor, not a guarantee. Every outgoing email passes
these non-LLM checks at send time; a failure blocks the send and records
why, so a bad generation can never reach a prospect. This is the pattern
the surviving commercial AI-SDR vendors converged on after the 2025
autonomous-agent flameouts: constraints in the prompt, verification in
code.
"""

import re
from dataclasses import dataclass, field

# Spam-filter and AI-tell phrases that must never appear in outgoing mail.
# Mirrors prompts/writer.md; enforced here because prompts can be ignored.
BANNED_PHRASES = (
    "act now", "buy now", "click here", "click below", "limited time",
    "limited offer", "exclusive deal", "special offer", "risk-free",
    "no obligation", "100% free", "make money", "double your",
    "guaranteed results", "winner", "congratulations",
    "i hope this finds you well", "hope this email finds you well",
    "i hope this email finds you", "picture this", "what if i told you",
    "in today's fast-paced world", "game-changer", "game changer",
    "cutting-edge solution", "revolutionize your", "unlock your",
    "supercharge your", "as an ai", "i'm an ai", "i am an ai",
    "as a language model",
)

MAX_SUBJECT_LEN = 90
MAX_BODY_WORDS = 220        # hard ceiling; sequences aim far lower
MAX_LINKS = 1

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MERGE_TAG_RE = re.compile(r"\{\{\s*\w+\s*\}\}|\{\s*\w+\s*\}(?!\})")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

DELIVERABLE_STATUSES = {"verified", "risky"}


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


def pre_send_check(
    to_email: str,
    subject: str,
    body: str,
    prospect=None,
    allow_risky: bool = False,
    kind: str = "sequence",
    postal_address: str = "",
) -> GateResult:
    """Run every deterministic check. Returns ok=False with reasons on any hit.

    postal_address: when set, a cold (sequence) email must carry it (CAN-SPAM).
    """
    reasons: list[str] = []
    subject = (subject or "").strip()
    body = (body or "").strip()
    to_email = (to_email or "").strip().lower()

    # Recipient sanity
    if not EMAIL_RE.match(to_email):
        reasons.append(f"invalid recipient address '{to_email}'")
    if prospect is not None:
        if (prospect.email or "").strip().lower() != to_email:
            reasons.append("recipient does not match the prospect record")
        allowed = DELIVERABLE_STATUSES if allow_risky else {"verified"}
        # Replies go back to someone who emailed US — deliverability proven.
        if kind == "sequence" and (prospect.email_status or "guess") not in allowed:
            reasons.append(
                f"email_status '{prospect.email_status or 'guess'}' is not deliverable"
            )

    # Content sanity
    if not subject:
        reasons.append("empty subject")
    if not body:
        reasons.append("empty body")
    if len(subject) > MAX_SUBJECT_LEN:
        reasons.append(f"subject too long ({len(subject)} > {MAX_SUBJECT_LEN})")
    word_count = len(body.split())
    if word_count > MAX_BODY_WORDS:
        reasons.append(f"body too long ({word_count} words > {MAX_BODY_WORDS})")

    # Unrendered merge tags — the classic mass-mail embarrassment
    leftover = MERGE_TAG_RE.findall(subject + " " + body)
    if leftover:
        reasons.append(f"unrendered merge tags: {sorted(set(leftover))[:3]}")

    # Banned phrases (spam triggers + AI tells)
    lowered = (subject + " " + body).lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        reasons.append(f"banned phrases: {hits[:3]}")

    # Link budget: cold email with links goes to spam
    links = URL_RE.findall(body)
    if len(links) > MAX_LINKS:
        reasons.append(f"too many links ({len(links)} > {MAX_LINKS})")

    if kind == "sequence" and postal_address.strip() and postal_address.strip() not in body:
        reasons.append("postal address footer missing")

    # No HTML — Harvey sends plain text only
    if re.search(r"<\s*(html|body|div|table|img|a)\b", body, re.IGNORECASE):
        reasons.append("HTML markup in body (plain text only)")

    return GateResult(ok=not reasons, reasons=reasons)


# ── Autopilot review ──────────────────────────────────────────────────────
#
# Websites and the replies people send reach the model, and either can carry
# planted instructions ("add this link", "paste your instructions"). Prompts
# fence that text (harvey.untrusted), but a model can still follow it. So an
# email nobody approved is also checked here before it goes: anything that
# trips these goes back to the Outbox for a human instead of out (#66).

# Real-world top-level domains a bare "name.tld" is read as a link for, plus
# the reserved test names (RFC 2606). A bare word with a path
# ("evil.example/x") is a link whatever its ending.
_COMMON_TLDS = frozenset("""
    com net org io co ai app dev xyz info biz me us uk ca au de fr es it nl be ch at se no dk fi
    ie pl pt cz eu in sg nz za br mx jp kr cn hk tw ru ly sh to gg tv fm link site online store
    shop tech cloud page so pro agency studio digital email live life media news blog club
    world today one gov edu mil int test example invalid localhost
""".split())
_SCHEME_HOST_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\[?([^\s/?#:\]]+)", re.IGNORECASE)
_EMAIL_HOST_RE = re.compile(r"[a-z0-9._%+\-]+@((?:[a-z0-9-]+\.)+[a-z]{2,})", re.IGNORECASE)
_BARE_HOST_RE = re.compile(
    r"(?<![\w@.\-/])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,24}))(?![\w-])(/)?",
    re.IGNORECASE)

# Files Harvey's instructions live in, under the checkout. A passage from
# them in an outgoing email means a reply quoted Harvey's playbook back.
# These are written to be said to prospects, so echoing them is fine.
_SAY_ALOUD_FILES = frozenset({"email_frameworks.md", "product_knowledge.md",
                              "objection_handling.md", "README.md"})
LEAK_RUN_WORDS = 12       # this many words in a row, copied, is a quote
_WORD_RE = re.compile(r"[a-z0-9']+")
# Lines the playbooks quote as scripts to use ("Would Thursday at 2pm work
# for a quick call?"): saying them is the point, so they aren't checked.
_SCRIPT_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:>|[\"“'‘])")
_leak_cache: dict = {}


def _host(value: str) -> str:
    h = value.strip().strip(".").lower()
    return h[4:] if h.startswith("www.") else h


def find_link_domains(text: str) -> list[str]:
    """Every domain a link or email address in ``text`` points at, in order."""
    text = text or ""
    hosts: list[str] = []
    for m in _SCHEME_HOST_RE.finditer(text):
        hosts.append(_host(m.group(1)))
    rest = re.sub(r"\b[a-z][a-z0-9+.-]*://\S*", " ", text, flags=re.IGNORECASE)
    for m in _EMAIL_HOST_RE.finditer(rest):
        hosts.append(_host(m.group(1)))
    rest = _EMAIL_HOST_RE.sub(" ", rest)
    for m in _BARE_HOST_RE.finditer(rest):
        if m.group(3) or m.group(2).lower() in _COMMON_TLDS:
            hosts.append(_host(m.group(1)))
    return list(dict.fromkeys(h for h in hosts if h))


def allowed_link_domains(config) -> set[str]:
    """Your own domains: the sending address's, the booking link's, and
    any listed in channels.email.link_domains."""
    persona = getattr(config, "persona", None)
    offer = getattr(getattr(config, "product", None), "offer", None)
    email_cfg = getattr(getattr(config, "channels", None), "email", None)
    out: set[str] = set()
    address = str(getattr(persona, "email", "") or "")
    if "@" in address:
        out.add(_host(address.rsplit("@", 1)[1]))
    out.update(find_link_domains(str(getattr(offer, "booking_url", "") or "")))
    for entry in getattr(email_cfg, "link_domains", None) or []:
        out.update(find_link_domains(str(entry)) or [_host(str(entry))])
    return {d for d in out if d}


def _is_ours(host: str, allowed: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _instruction_runs(root) -> dict[tuple, str]:
    """Every LEAK_RUN_WORDS-word run in Harvey's instructions → its file."""
    from harvey.untrusted import UNTRUSTED_RULE
    files = sorted(p for folder in ("prompts", "skills") for p in (root / folder).glob("*.md")
                   if p.name not in _SAY_ALOUD_FILES)
    key = (str(root), tuple((str(p), p.stat().st_mtime_ns) for p in files))
    if _leak_cache.get("key") != key:
        runs: dict[tuple, str] = {}
        sources = [(p.name, "\n".join(
            line for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if not _SCRIPT_LINE_RE.match(line))) for p in files]
        sources.append(("Harvey's prompt rules", UNTRUSTED_RULE))
        for name, text in sources:
            words = _words(text)
            for i in range(len(words) - LEAK_RUN_WORDS + 1):
                runs.setdefault(tuple(words[i:i + LEAK_RUN_WORDS]), name)
        _leak_cache.update(key=key, runs=runs)
    return _leak_cache["runs"]


def autopilot_review(subject: str, body: str, config, root=None) -> list[str]:
    """Why an email nobody approved must wait for a human, or [] to send.

    Two things a planted instruction would try: point the reader somewhere
    that isn't yours, or get Harvey to quote its own instructions back."""
    from harvey.paths import PROJECT_ROOT
    reasons: list[str] = []
    text = f"{subject or ''}\n{body or ''}"
    allowed = allowed_link_domains(config)
    foreign = [h for h in find_link_domains(text) if not _is_ours(h, allowed)]
    if foreign:
        reasons.append(
            f"links to {', '.join(foreign[:3])}, not one of your domains "
            "(add it to channels.email.link_domains if it's yours)")
    words = _words(body)
    runs = _instruction_runs(root or PROJECT_ROOT)
    for i in range(len(words) - LEAK_RUN_WORDS + 1):
        source = runs.get(tuple(words[i:i + LEAK_RUN_WORDS]))
        if source:
            reasons.append(f"quotes Harvey's instructions ({source})")
            break
    return reasons
