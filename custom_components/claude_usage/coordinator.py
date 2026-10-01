"""Data update coordinator for Claude Usage."""

from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    AdminUsage,
    ClaudeAuthError,
    ClaudeRateLimitError,
    ClaudeUsageError,
    SubscriptionUsage,
    fetch_admin_usage,
    fetch_subscription_usage,
    refresh_tokens,
)
from .const import (
    AUTH_ADMIN,
    CONF_ACCESS_TOKEN,
    CONF_ADMIN_KEY,
    CONF_AUTH_TYPE,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_ADMIN,
    DEFAULT_SCAN_SUBSCRIPTION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type ClaudeUsageConfigEntry = ConfigEntry[ClaudeUsageCoordinator]


class ClaudeUsageCoordinator(DataUpdateCoordinator[SubscriptionUsage | AdminUsage]):
    """Polls either the subscription usage endpoint or the Admin API."""

    config_entry: ClaudeUsageConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ClaudeUsageConfigEntry) -> None:
        self.auth_type: str = entry.data[CONF_AUTH_TYPE]
        default = DEFAULT_SCAN_ADMIN if self.auth_type == AUTH_ADMIN else DEFAULT_SCAN_SUBSCRIPTION
        seconds = entry.options.get(CONF_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=seconds) if seconds else default,
        )
        self._session = async_get_clientsession(hass)
        self.options_snapshot = dict(entry.options)

    async def _async_update_data(self) -> SubscriptionUsage | AdminUsage:
        try:
            if self.auth_type == AUTH_ADMIN:
                return await fetch_admin_usage(
                    self._session, self.config_entry.data[CONF_ADMIN_KEY]
                )
            return await self._update_subscription()
        except ClaudeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClaudeRateLimitError as err:
            raise UpdateFailed(f"Rate limited, will retry: {err}") from err
        except ClaudeUsageError as err:
            raise UpdateFailed(str(err)) from err

    async def _update_subscription(self) -> SubscriptionUsage:
        data = self.config_entry.data
        if float(data.get(CONF_EXPIRES_AT, 0)) - 300 < time.time():
            await self._refresh()
        try:
            return await fetch_subscription_usage(
                self._session, self.config_entry.data[CONF_ACCESS_TOKEN]
            )
        except ClaudeAuthError:
            # Access token may have been revoked early; try one refresh.
            await self._refresh()
            return await fetch_subscription_usage(
                self._session, self.config_entry.data[CONF_ACCESS_TOKEN]
            )

    async def _refresh(self) -> None:
        _LOGGER.debug("Refreshing Claude OAuth token")
        tokens = await refresh_tokens(
            self._session, self.config_entry.data[CONF_REFRESH_TOKEN]
        )
        new_data: dict[str, Any] = {
            **self.config_entry.data,
            CONF_ACCESS_TOKEN: tokens.access_token,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_EXPIRES_AT: tokens.expires_at,
        }
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
