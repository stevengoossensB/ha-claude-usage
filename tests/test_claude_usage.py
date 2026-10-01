"""Tests for the Claude Usage integration."""

from __future__ import annotations

import time

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.claude_usage.api import parse_pasted_code, parse_subscription_usage
from custom_components.claude_usage.const import (
    ADMIN_BASE_URL,
    DOMAIN,
    OAUTH_TOKEN_URL,
    USAGE_URL,
)

USAGE = {
    "five_hour": {"utilization": 9.0, "resets_at": "2026-09-14T02:10:00Z"},
    "seven_day": {"utilization": 68.0, "resets_at": "2026-09-19T09:00:00Z"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250, "utilization": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 9, "severity": "normal", "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 68, "severity": "normal", "is_active": False},
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 100,
            "severity": "critical",
            "is_active": True,
            "resets_at": "2026-09-19T09:00:00Z",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
}


def test_parse_usage() -> None:
    parsed = parse_subscription_usage(USAGE)
    assert set(parsed.windows) == {"five_hour", "seven_day", "weekly_fable"}
    assert parsed.windows["seven_day"].utilization == 68.0
    assert parsed.windows["weekly_fable"].extra["severity"] == "critical"
    assert parsed.windows["five_hour"].resets_at.hour == 2


def test_parse_code() -> None:
    assert parse_pasted_code(" abc#xyz ") == ("abc", "xyz")
    assert parse_pasted_code("https://x/cb?code=c1&state=s1") == ("c1", "s1")
    assert parse_pasted_code("plain") == ("plain", None)


async def test_subscription_flow_and_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(
        OAUTH_TOKEN_URL,
        json={
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
            "account": {"uuid": "acc-1", "email_address": "me@example.com"},
        },
    )
    aioclient_mock.get(USAGE_URL, json=USAGE)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "subscription"}
    )
    assert result["type"] is FlowResultType.FORM
    assert "claude.ai/oauth/authorize" in result["description_placeholders"]["auth_url"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "the-code"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["title"] == "Claude me@example.com"
    await hass.async_block_till_done()

    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.claude_me_example_com_session_5h_usage"] == "9.0"
    assert states["sensor.claude_me_example_com_weekly_usage"] == "68.0"
    assert states["sensor.claude_me_example_com_weekly_fable_usage"] == "100.0"
    assert states["sensor.claude_me_example_com_extra_usage"] == "25.0"
    assert states["sensor.claude_me_example_com_weekly_reset"].startswith("2026-09-19T09:00:00")


async def test_expired_token_is_refreshed(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(
        OAUTH_TOKEN_URL,
        json={"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600},
    )
    aioclient_mock.get(USAGE_URL, json=USAGE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Claude",
        unique_id="sub_x",
        data={
            "auth_type": "subscription",
            "access_token": "old",
            "refresh_token": "old-rt",
            "expires_at": time.time() - 10,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.data["access_token"] == "new-at"
    assert entry.data["refresh_token"] == "new-rt"
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_refresh_failure_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(OAUTH_TOKEN_URL, status=400, text='{"error":"invalid_grant"}')
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="sub_y",
        data={
            "auth_type": "subscription",
            "access_token": "old",
            "refresh_token": "bad",
            "expires_at": 0,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_admin_flow_and_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(f"{ADMIN_BASE_URL}/me", json={"id": "org-1", "name": "Acme"})
    today = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    aioclient_mock.get(
        f"{ADMIN_BASE_URL}/usage_report/messages",
        json={
            "data": [
                {
                    "starting_at": "2000-01-01T00:00:00Z",
                    "results": [
                        {"uncached_input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10,
                         "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 0},
                         "model": "claude-x"}
                    ],
                },
                {
                    "starting_at": today,
                    "results": [{"uncached_input_tokens": 1, "output_tokens": 2, "model": "claude-x"}],
                },
            ],
            "has_more": False,
        },
    )
    aioclient_mock.get(
        f"{ADMIN_BASE_URL}/cost_report",
        json={"data": [{"starting_at": today, "results": [{"amount": "250", "currency": "USD"}]}], "has_more": False},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "admin_api"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"admin_key": "sk-ant-admin-x"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Anthropic API Acme"
    await hass.async_block_till_done()
    states = {s.entity_id: s.state for s in hass.states.async_all("sensor")}
    assert states["sensor.anthropic_api_acme_total_tokens_this_month"] == "168"
    assert states["sensor.anthropic_api_acme_total_tokens_today"] == "3"
    assert states["sensor.anthropic_api_acme_cost_this_month"] == "2.5"


async def test_login_rate_limited_shows_specific_error(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(OAUTH_TOKEN_URL, status=429, json={"error": {"type": "rate_limit_error"}})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "subscription"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "c#s"})
    # state "s" doesn't match, so use a code without state
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "c"})
    assert result["errors"] == {"base": "rate_limited"}


def test_user_agent_is_not_claude_code() -> None:
    from custom_components.claude_usage.const import USER_AGENT

    assert not USER_AGENT.startswith("claude-")
