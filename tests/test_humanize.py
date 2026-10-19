"""The Humanizer pass rewrites drafts but never lets a bad rewrite through."""

import pytest

from harvey.humanize import humanize, load_skill


class FakeBrain:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def think_json(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def skill(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: humanizer\n---\n\n# Humanizer\nCut the tells.\n")
    return path


DRAFT = {"subject": "missed calls", "body": "Hi {{first_name}}, it's not about calls. It's about jobs. Who answers after five?"}


def test_load_skill_strips_frontmatter(skill):
    assert load_skill(skill) == "# Humanizer\nCut the tells."


def test_missing_skill_is_a_no_op(tmp_path):
    assert load_skill(tmp_path / "nope.md") == ""


@pytest.mark.asyncio
async def test_no_skill_makes_no_call(tmp_path):
    brain = FakeBrain([])
    assert await humanize(brain, [DRAFT], "writer", path=tmp_path / "nope.md") == [DRAFT]
    assert brain.prompts == []


@pytest.mark.asyncio
async def test_good_rewrite_is_used(skill):
    rewrite = {"subject": "after five", "body": "Hi {{first_name}}, who picks up the phone after five?"}
    brain = FakeBrain([rewrite])
    out = await humanize(brain, [DRAFT], "writer", path=skill)
    assert out == [rewrite]
    assert "Cut the tells." in brain.prompts[0]


@pytest.mark.parametrize("bad", [
    {"subject": "after five", "body": "Hi there, who picks up after five?"},           # lost merge var
    {"subject": "after five", "body": "Hi {{first_name}} — who picks up after five?"},  # new em dash
    {"subject": "", "body": "Hi {{first_name}}, who picks up after five?"},            # dropped subject
    {"subject": "after five", "body": "Hi {{first_name}}, " + "word " * 40},          # much longer
    "not a dict",
])
@pytest.mark.asyncio
async def test_bad_rewrite_keeps_original(skill, bad):
    out = await humanize(FakeBrain([bad]), [DRAFT], "writer", path=skill)
    assert out == [DRAFT]


@pytest.mark.asyncio
async def test_wrong_shape_or_failure_keeps_all(skill):
    assert await humanize(FakeBrain([]), [DRAFT], "writer", path=skill) == [DRAFT]
    assert await humanize(FakeBrain(None), [DRAFT], "writer", path=skill) == [DRAFT]
    assert await humanize(FakeBrain(RuntimeError("down")), [DRAFT], "writer", path=skill) == [DRAFT]


@pytest.mark.asyncio
async def test_reply_without_subject(skill):
    reply = {"subject": "", "body": "Sure, Thursday works. What time suits you?"}
    out = await humanize(FakeBrain([reply]), [{"subject": "", "body": "Sure! Thursday works for me. What time suits you best?"}], "handler", path=skill)
    assert out == [reply]
