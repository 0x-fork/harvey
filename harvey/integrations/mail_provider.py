"""Mail provider abstraction — Harvey owns its sending, Instantly optional.

A MailProvider gives Harvey full inbox control: send, read replies,
thread. Two native providers ship:

- ``gmail``  — a Google Workspace / Gmail mailbox via the Gmail REST API
  (recommended: a real mailbox on a dedicated secondary domain is the
  most deliverable way to send <50 cold emails/day).
- ``microsoft`` — a Microsoft 365 / Outlook mailbox via Microsoft Graph
  (OAuth; Exchange Online no longer accepts passwords over IMAP).
- ``smtp``   — any mailbox that speaks SMTP + IMAP (AgentMail, Fastmail,
  a Workspace app-mailbox, self-hosted). Configuration via .env.

``instantly`` remains supported through the legacy Sender/Handler path —
it isn't a MailProvider because Instantly owns sequencing/sending itself.
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("harvey.mail")

NATIVE_PROVIDERS = ("gmail", "smtp", "microsoft")


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""     # RFC Message-ID (or provider id) of the sent mail
    thread_ref: str = ""     # provider thread handle (Gmail threadId, etc.)
    error: str = ""


@dataclass
class InboundMessage:
    provider_id: str         # provider-unique id, used for reply dedup
    from_email: str
    subject: str = ""
    body: str = ""
    message_id: str = ""     # RFC Message-ID header
    in_reply_to: str = ""    # RFC In-Reply-To header (bounce/thread matching)
    thread_ref: str = ""
    date: str = ""
    is_bounce: bool = False
    headers: dict = field(default_factory=dict)


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found",
)


# Where a reply starts quoting the email it answers. Clients wrap the
# "On ... wrote:" line, so one line break is allowed inside it.
_QUOTE_HEADERS = (
    # Gmail / Apple Mail, and the Spanish, German and French equivalents
    re.compile(r"^[ \t]*On\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bwrote:[ \t]*$", re.MULTILINE),
    re.compile(r"^[ \t]*El\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bescribi[oó]:[ \t]*$", re.MULTILINE),
    re.compile(r"^[ \t]*Am\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bschrieb[^\n]{0,100}:[ \t]*$", re.MULTILINE),
    re.compile(r"^[ \t]*Le\b[^\n]{0,300}(?:\n[^\n]{0,300})?\ba [ée]crit[ \t]*:[ \t]*$", re.MULTILINE),
    # Outlook
    re.compile(r"^[ \t]*-{2,}[ \t]*Original Message[ \t]*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^[ \t]*From:[^\n]*\n[ \t]*(?:Sent|Date):", re.MULTILINE),
    re.compile(r"^[ \t]*_{10,}[ \t]*$", re.MULTILINE),
)


def strip_quoted_reply(body: str) -> str:
    """What the sender wrote, without the email they're answering.

    ">" lines are dropped wherever they are, so answers written between
    quoted lines are kept; everything from the first quote header ("On ...
    wrote:", Outlook's From:/Sent: block) is cut. When that would leave
    nothing (they answered under the header), their unquoted lines are kept.
    Used for reading a reply; opt-out detection looks wider (see handler).
    """
    text = (body or "").replace("\r\n", "\n")
    text = "\n".join(
        line for line in text.split("\n") if not line.lstrip().startswith(">"))
    cut = len(text)
    for pattern in _QUOTE_HEADERS:
        m = pattern.search(text)
        if m and m.start() < cut:
            cut = m.start()
    kept = text[:cut].strip() or text.strip()
    return re.sub(r"\n{3,}", "\n\n", kept)


def looks_like_bounce(from_email: str, subject: str) -> bool:
    f = (from_email or "").lower()
    s = (subject or "").lower()
    return any(m in f for m in BOUNCE_SENDERS) or any(m in s for m in BOUNCE_SUBJECTS)


class MailProvider(ABC):
    """A mailbox Harvey can send from and read replies out of."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether required credentials are present (no network)."""

    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        """Send one plain-text email. Reply threading when refs are given."""

    @abstractmethod
    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Fetch recent inbound messages (dedup is the caller's job)."""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Cheap end-to-end credential check. Returns (ok, detail)."""


def get_mail_provider(config, env) -> MailProvider | None:
    """Build the configured native provider, or None for instantly/unknown."""
    provider = (config.channels.email.provider or "").strip().lower()
    if provider == "gmail":
        from harvey.integrations.gmail import GmailProvider
        return GmailProvider(config, env)
    if provider == "smtp":
        from harvey.integrations.smtp_mail import SmtpImapProvider
        return SmtpImapProvider(config, env)
    if provider == "microsoft":
        from harvey.integrations.microsoft import MicrosoftProvider
        return MicrosoftProvider(config, env)
    return None
