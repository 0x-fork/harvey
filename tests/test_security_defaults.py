"""Approval requirements apply to legacy providers; LinkedIn needs opt-in."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import yaml

from harvey.agents.handler import Handler
from harvey.agents.sender import Sender
from harvey.config import BrainConfig, EnvConfig, LinkedInChannelConfig
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager
from harvey.trainer import Trainer


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    return state


def legacy_config(require_approval=True):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=require_approval,
            max_daily_sends=50, send_to_risky=False)),
        persona=SimpleNamespace(name="Harvey", role="BDR", company="Acme", tone="direct"),
        product=SimpleNamespace(name="Widget", description="A widget", objection_responses={}),
    )


@pytest.mark.asyncio
async def test_legacy_campaign_requires_explicit_autopilot_even_on_activation_retry(state):
    sender = Sender(None, state, legacy_config(), EnvConfig())
    sender.instantly = AsyncMock()
    campaign = Campaign(id="c", name="Draft", instantly_campaign_id="existing-campaign",
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    await sender._deploy_campaign(campaign)
    assert sender.instantly.mock_calls == []


@pytest.mark.asyncio
async def test_legacy_campaign_autopilot_is_still_available(state):
    sender = Sender(None, state, legacy_config(False), EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = None
    campaign = Campaign(id="c", name="Draft",
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    await sender._deploy_campaign(campaign)
    sender.instantly.create_campaign.assert_awaited_once_with("Draft")


@pytest.mark.asyncio
@pytest.mark.parametrize("require_approval", [True, False])
async def test_legacy_replies_respect_approval_and_record_inbound_messages(state, require_approval):
    pid = await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", company="Acme",
                                          title="Owner", email="jane@acme.test"))
    handler = Handler(None, state, legacy_config(require_approval), EnvConfig())
    handler.instantly = AsyncMock()
    handler._classify_intent = AsyncMock(return_value="question")
    handler._generate_response = AsyncMock(return_value="Happy to help.")
    await handler._process_reply("jane@acme.test", "What does it cost?", "reply-id")
    if require_approval:
        handler.instantly.send_reply.assert_not_awaited()
        [convo] = await state.get_conversations_by_status("needs_human")
        assert convo.prospect_id == pid and convo.thread[-1].content == "What does it cost?"
    else:
        handler.instantly.send_reply.assert_awaited_once_with("reply-id", "Happy to help.")


@pytest.mark.asyncio
async def test_legacy_opt_out_is_honored_even_when_approval_blocks_sending(state):
    pid = await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Owner",
                                          email="jane@acme.test"))
    handler = Handler(None, state, legacy_config(), EnvConfig())
    handler.instantly = AsyncMock()
    await handler._process_reply("jane@acme.test", "Please unsubscribe me", "reply-id")
    assert (await state.get_prospect(pid)).status == "opted_out"
    handler.instantly.send_reply.assert_not_awaited()


def test_linkedin_is_disabled_in_model_and_shipped_template():
    assert LinkedInChannelConfig().enabled is False
    template = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text())
    assert template["channels"]["linkedin"]["enabled"] is False
    assert LinkedInChannelConfig(enabled=True).enabled is True


def test_product_training_does_not_enable_linkedin():
    trainer = Trainer.__new__(Trainer)
    trainer.brain_config = BrainConfig()
    config = trainer._build_config({}, {}, {}, "acme.test")
    assert config["channels"]["linkedin"]["enabled"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_setup_saves_explicit_linkedin_choice_for_existing_config(tmp_path, monkeypatch, enabled):
    from harvey import setup
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({"channels": {"linkedin": {"enabled": not enabled}}}))
    monkeypatch.setattr(setup, "CONFIG_FILE", path)
    wizard = setup.SetupWizard()
    wizard.env_vars["LINKEDIN_EMAIL"] = "user@example.test" if enabled else ""
    wizard.config = {"channels": {"linkedin": {"enabled": enabled}}}
    wizard._write_config()
    assert yaml.safe_load(path.read_text())["channels"]["linkedin"]["enabled"] is enabled
