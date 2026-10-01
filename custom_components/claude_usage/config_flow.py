"""Config flow for Claude Usage."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    ClaudeAuthError,
    ClaudeUsageError,
    build_authorize_url,
    exchange_code,
    fetch_profile,
    fetch_subscription_usage,
    generate_pkce,
    parse_pasted_code,
    validate_admin_key,
)
from .const import (
    AUTH_ADMIN,
    AUTH_SUBSCRIPTION,
    CONF_ACCESS_TOKEN,
    CONF_ADMIN_KEY,
    CONF_AUTH_TYPE,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_ADMIN,
    DEFAULT_SCAN_SUBSCRIPTION,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"


class ClaudeUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None

    # ------------------------------------------------------------------ menu
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user pick subscription limits or Admin API usage."""
        return self.async_show_menu(step_id="user", menu_options=[AUTH_SUBSCRIPTION, AUTH_ADMIN])

    # ---------------------------------------------------------- subscription
    async def async_step_subscription(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """OAuth (PKCE, manual code paste) login to a Claude Pro/Max account."""
        errors: dict[str, str] = {}
        if self._verifier is None:
            self._verifier, challenge = generate_pkce()
            self._state = secrets.token_urlsafe(24)
            self._auth_url = build_authorize_url(challenge, self._state)

        if user_input is not None:
            code, pasted_state = parse_pasted_code(user_input[CONF_CODE])
            if pasted_state and pasted_state != self._state:
                errors["base"] = "state_mismatch"
            elif not code:
                errors["base"] = "invalid_code"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    tokens = await exchange_code(session, code, self._verifier, self._state)
                    await fetch_subscription_usage(session, tokens.access_token)
                except ClaudeAuthError as err:
                    _LOGGER.warning("Claude login failed: %s", err)
                    errors["base"] = "invalid_auth"
                except ClaudeUsageError as err:
                    _LOGGER.warning("Claude login failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    email = tokens.account_email
                    account_id = tokens.account_id
                    if not email:
                        profile = await fetch_profile(session, tokens.access_token)
                        account = profile.get("account") or {}
                        email = account.get("email") or account.get("email_address")
                        account_id = account_id or account.get("uuid")
                    data = {
                        CONF_AUTH_TYPE: AUTH_SUBSCRIPTION,
                        CONF_ACCESS_TOKEN: tokens.access_token,
                        CONF_REFRESH_TOKEN: tokens.refresh_token,
                        CONF_EXPIRES_AT: tokens.expires_at,
                    }
                    unique = f"sub_{account_id or email or tokens.refresh_token[-12:]}"
                    return await self._finish(unique, f"Claude {email or 'subscription'}", data)
            # A code can only be used once: start a fresh PKCE round on error.
            self._verifier, challenge = generate_pkce()
            self._state = secrets.token_urlsafe(24)
            self._auth_url = build_authorize_url(challenge, self._state)

        return self.async_show_form(
            step_id="subscription",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            description_placeholders={"auth_url": self._auth_url or ""},
            errors=errors,
        )

    # ----------------------------------------------------------------- admin
    async def async_step_admin_api(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Anthropic Admin API key for organisation API usage and cost."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_ADMIN_KEY].strip()
            session = async_get_clientsession(self.hass)
            try:
                org = await validate_admin_key(session, key)
            except ClaudeAuthError:
                errors["base"] = "invalid_auth"
            except ClaudeUsageError as err:
                _LOGGER.warning("Admin API validation failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                org_id = org.get("id") or hashlib.sha256(key.encode()).hexdigest()[:16]
                title = f"Anthropic API {org.get('name') or ''}".strip()
                return await self._finish(
                    f"admin_{org_id}", title, {CONF_AUTH_TYPE: AUTH_ADMIN, CONF_ADMIN_KEY: key}
                )
        return self.async_show_form(
            step_id="admin_api",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADMIN_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    # ---------------------------------------------------------------- shared
    async def _finish(self, unique_id: str, title: str, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data_updates=data)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(title=title, data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Credentials expired or were revoked."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route to the right login step."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        if self._get_reauth_entry().data.get(CONF_AUTH_TYPE) == AUTH_ADMIN:
            return await self.async_step_admin_api()
        return await self.async_step_subscription()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return ClaudeUsageOptionsFlow()


class ClaudeUsageOptionsFlow(OptionsFlow):
    """Polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        default = (
            DEFAULT_SCAN_ADMIN
            if self.config_entry.data.get(CONF_AUTH_TYPE) == AUTH_ADMIN
            else DEFAULT_SCAN_SUBSCRIPTION
        )
        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, int(default.total_seconds()))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=86400,
                            step=30,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
