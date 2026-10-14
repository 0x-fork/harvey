"""Optional per-recipient cold-email check through Precleared (precleared.dev).

Off by default. With ``compliance.precleared.enabled: true``, the sender asks
Precleared about each new recipient before the first cold email of a
sequence: may a cold marketing email go to this address under UK PECR, US
CAN-SPAM, Canada's CASL, Germany's UWG or France's rules? Anything other than
``ok_to_email`` holds the email back with the reason, and the signed receipt
id is logged with the send so the decision can be shown later.

One check per address: a definite verdict is cached in the settings table for
CACHE_DAYS, so a three-step sequence costs one check. Replies are never
checked. Precleared receives the recipient's address, your sender country,
the address source you configure and your product description (used to judge
relevance under CASL and French rules).

Without an API key Precleared allows 10 checks a day; a free key
(https://precleared.dev/get-key, PRECLEARED_API_KEY) allows 25 a day.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("harvey.precleared")

API_URL = "https://precleared.dev/api/v1/check"
# Precleared answers within 25 seconds (with verdict "unknown" if a register
# lookup is slow), so the read timeout sits just above that.
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
CACHE_DAYS = 30
SETTINGS_PREFIX = "precleared:"
DEFINITE_VERDICTS = {"ok_to_email", "consent_needed", "blocked"}
ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")
SENDER_COUNTRIES = ("GB", "US")


@dataclass
class Clearance:
    """Precleared's answer for one address.

    ``allowed``: send it. ``held``: the check could not be made right now
    (network trouble, daily allowance used up, bad key), so the email stays
    queued and is tried again on a later cycle. Otherwise the email is
    blocked and ``reason`` says why.
    """

    allowed: bool
    held: bool = False
    verdict: str = ""
    reason: str = ""
    receipt_id: str = ""


class PreclearedClient:
    def __init__(
        self,
        sender_country: str,
        api_key: str = "",
        address_source: str = "unknown",
        pitch: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.sender_country = sender_country
        self.api_key = api_key or ""
        self.address_source = address_source
        self.pitch = (pitch or "").strip()[:200]
        self._transport = transport  # tests inject httpx.MockTransport

    @classmethod
    def from_config(cls, config, env) -> "PreclearedClient | None":
        """Build a client when the check is switched on, else return None."""
        compliance = getattr(config, "compliance", None)
        settings = getattr(compliance, "precleared", None)
        if settings is None or getattr(settings, "enabled", False) is not True:
            return None
        product = getattr(config, "product", None)
        return cls(
            sender_country=settings.sender_country,
            api_key=getattr(env, "precleared_api_key", ""),
            address_source=settings.address_source,
            pitch=getattr(product, "description", ""),
        )

    def _redact(self, text: str) -> str:
        if self.api_key and text:
            return text.replace(self.api_key, "***REDACTED***")
        return text or ""

    async def clear(self, to_email: str, state) -> Clearance:
        """Return the verdict for one address, from the cache when possible."""
        email = (to_email or "").strip().lower()
        key = SETTINGS_PREFIX + email
        cached = _from_cache(await state.get_setting(key))
        if cached is not None:
            return cached

        result = await self._check(email)
        if not result.held and result.verdict in DEFINITE_VERDICTS:
            await state.set_setting(key, json.dumps({
                "verdict": result.verdict,
                "reason": result.reason,
                "receipt_id": result.receipt_id,
                "checked_at": _utcnow().isoformat(),
            }))
        return result

    async def _check(self, email: str) -> Clearance:
        payload = {
            "recipient_email": email,
            "sender_country": self.sender_country,
            "address_source": self.address_source,
            "existing_customer": False,
        }
        if self.pitch:
            payload["pitch"] = self.pitch
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "harvey (+https://github.com/ethanplusai/harvey)",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                resp = await client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return Clearance(allowed=False, held=True,
                             reason=self._redact(f"Precleared unreachable: {e}"))

        if resp.status_code in (402, 429):
            return Clearance(
                allowed=False, held=True,
                reason="Precleared daily allowance used up; add PRECLEARED_API_KEY "
                       "(free key: https://precleared.dev/get-key) or wait until tomorrow",
            )
        if resp.status_code == 401:
            return Clearance(allowed=False, held=True,
                             reason="PRECLEARED_API_KEY was rejected (wrong or revoked)")
        if resp.status_code != 200:
            return Clearance(allowed=False, held=True,
                             reason=self._redact(f"Precleared HTTP {resp.status_code}: {resp.text[:200]}"))

        try:
            data = resp.json()
        except ValueError:
            return Clearance(allowed=False, held=True, reason="Precleared returned invalid JSON")

        verdict = str(data.get("verdict") or "")
        receipt = data.get("receipt") or {}
        receipt_id = str(receipt.get("id") or "") if isinstance(receipt, dict) else ""
        if verdict == "ok_to_email":
            return Clearance(allowed=True, verdict=verdict, receipt_id=receipt_id)
        return Clearance(
            allowed=False,
            verdict=verdict or "unknown",
            reason=_reason(data),
            receipt_id=receipt_id,
        )


def _reason(data: dict) -> str:
    """One readable line: the rule that decided it (or the note), then the next step.

    Precleared's ``reasons`` lead with the matching rule as
    ``{"rule_key", "text", "source_title", "source_url"}``, or with a
    ``{"note": ...}`` when no rule could be applied.
    """
    parts = []
    reasons = data.get("reasons") or []
    first = reasons[0] if reasons and isinstance(reasons[0], dict) else {}
    text = str(first.get("text") or first.get("note") or "").strip()
    source = str(first.get("source_title") or "").strip()
    if text:
        parts.append(f"{text} ({source})" if source else text)
    next_step = str(data.get("next_step") or "").strip()
    if next_step:
        parts.append(next_step)
    return " ".join(parts)[:240]


def _from_cache(raw: str) -> Clearance | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        checked_at = datetime.fromisoformat(data["checked_at"])
    except (ValueError, KeyError, TypeError):
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    if _utcnow() - checked_at > timedelta(days=CACHE_DAYS):
        return None
    verdict = data.get("verdict", "")
    return Clearance(
        allowed=verdict == "ok_to_email",
        verdict=verdict,
        reason=data.get("reason", ""),
        receipt_id=data.get("receipt_id", ""),
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
