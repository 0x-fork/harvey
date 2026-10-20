"""Humanizer pass — rewrites drafts with the operator's installed Humanizer skill.

Harvey's brain calls run with no tools and no skills (see brain.py), so a
Claude Code skill can't be invoked directly. Instead the skill's
instructions are read from the operator's skills folder and sent as a
second, editing-only call over drafts that are already written.

Fails open: no skill file, a failed call, or a rewrite that breaks a rule
(lost merge variable, new em dash, much longer) keeps the original draft.
Every draft still passes the deterministic pre-send gate afterwards.
"""

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("harvey.humanize")

SKILL_PATH = Path(
    os.getenv(
        "HARVEY_HUMANIZER_SKILL",
        str(Path.home() / ".claude" / "skills" / "humanizer" / "SKILL.md"),
    )
)
MERGE_VAR = re.compile(r"\{\{\s*\w+\s*\}\}")
MAX_GROWTH = 1.2  # a rewrite may not be more than 20% longer than the draft


def load_skill(path: Path | None = None) -> str:
    """The skill's instructions without its YAML frontmatter, or ""."""
    try:
        text = (path or SKILL_PATH).read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def _acceptable(original: dict, rewrite) -> bool:
    if not isinstance(rewrite, dict):
        return False
    body = str(rewrite.get("body") or "").strip()
    subject = str(rewrite.get("subject") or "").strip()
    if not body or (original.get("subject") and not subject):
        return False
    before = original.get("subject", "") + original["body"]
    after = subject + body
    if sorted(MERGE_VAR.findall(before)) != sorted(MERGE_VAR.findall(after)):
        return False
    if "—" in after and "—" not in before:
        return False
    return len(body.split()) <= len(original["body"].split()) * MAX_GROWTH + 2


async def humanize(brain, drafts: list[dict], agent: str, path: Path | None = None) -> list[dict]:
    """Return ``drafts`` ({"subject", "body"} dicts) rewritten by the skill.

    One brain call for the whole list. Any draft whose rewrite fails a check
    comes back unchanged, so the result always has the same length and order.
    """
    skill = load_skill(path)
    if not drafts or not skill:
        return drafts

    payload = [{"subject": d.get("subject", ""), "body": d["body"]} for d in drafts]
    prompt = f"""{skill}

---

Apply the Humanizer instructions above to the cold-email drafts below. They
are short plain-text business emails, so return email text only.

Hard limits on the rewrite, on top of the instructions above:
- Same number of items, same order.
- Keep every merge variable such as {{{{first_name}}}} or {{{{company}}}} exactly as written.
- Add no fact, name, number or claim that is not already in the draft.
- No em dashes, no bullets, no bold, no exclamation marks.
- Do not make any email longer. Keep exactly one question per email.
- Subjects stay lowercase, 2-5 words. An empty subject stays empty.

Drafts:
{json.dumps(payload, ensure_ascii=False, indent=2)}

Return ONLY a JSON array: [{{"subject": "...", "body": "..."}}, ...]"""

    try:
        result = await brain.think_json(
            prompt, session_id="harvey-humanizer", agent=agent, task="humanize",
        )
    except Exception as e:
        logger.warning(f"Humanizer pass failed, keeping drafts: {e}")
        return drafts
    if not isinstance(result, list) or len(result) != len(drafts):
        logger.warning("Humanizer pass returned the wrong shape, keeping drafts.")
        return drafts

    out = []
    for original, rewrite in zip(drafts, result):
        if _acceptable(original, rewrite):
            out.append({
                **original,
                "subject": str(rewrite.get("subject") or "").strip(),
                "body": str(rewrite["body"]).strip(),
            })
        else:
            out.append(original)
    kept = sum(1 for a, b in zip(drafts, out) if a is b)
    if kept:
        logger.info(f"Humanizer: kept {kept} of {len(drafts)} draft(s) unchanged after checks.")
    return out
