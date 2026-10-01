"""Sensors for Claude Usage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import TOKEN_FIELDS, AdminUsage, SubscriptionUsage
from .const import AUTH_ADMIN, DOMAIN
from .coordinator import ClaudeUsageConfigEntry, ClaudeUsageCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaudeUsageConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors."""
    coordinator = entry.runtime_data
    if coordinator.auth_type == AUTH_ADMIN:
        async_add_entities(
            AdminSensor(coordinator, desc) for desc in ADMIN_SENSORS
        )
        return

    known: set[str] = set()

    @callback
    def _add_new_windows() -> None:
        data = coordinator.data
        if not isinstance(data, SubscriptionUsage):
            return
        new: list[SensorEntity] = []
        for key, window in data.windows.items():
            if key in known:
                continue
            known.add(key)
            new.append(WindowUtilizationSensor(coordinator, key, window.name))
            new.append(WindowResetSensor(coordinator, key, window.name))
        if data.extra_usage is not None and "extra_usage" not in known:
            known.add("extra_usage")
            new.append(ExtraUsageSensor(coordinator))
        if new:
            async_add_entities(new)

    _add_new_windows()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_windows))


class ClaudeEntity(CoordinatorEntity[ClaudeUsageCoordinator]):
    """Base entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ClaudeUsageCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Anthropic",
            model="Admin API" if coordinator.auth_type == AUTH_ADMIN else "Claude subscription",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://claude.ai/settings/usage"
            if coordinator.auth_type != AUTH_ADMIN
            else "https://platform.claude.com/usage",
        )


# ----------------------------------------------------------------- subscription


class WindowUtilizationSensor(ClaudeEntity, SensorEntity):
    """Percent of a usage window consumed."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:gauge"

    def __init__(self, coordinator: ClaudeUsageCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"{key}_utilization")
        self._key = key
        self._attr_name = f"{name} usage"

    @property
    def _window(self):
        data = self.coordinator.data
        return data.windows.get(self._key) if isinstance(data, SubscriptionUsage) else None

    @property
    def available(self) -> bool:
        return super().available and self._window is not None

    @property
    def native_value(self) -> float | None:
        window = self._window
        return window.utilization if window else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        window = self._window
        if not window:
            return None
        attrs: dict[str, Any] = {
            "resets_at": window.resets_at.isoformat() if window.resets_at else None
        }
        attrs.update({k: v for k, v in window.extra.items() if v is not None})
        return attrs


class WindowResetSensor(ClaudeEntity, SensorEntity):
    """When a usage window resets."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-refresh-outline"

    def __init__(self, coordinator: ClaudeUsageCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, f"{key}_resets_at")
        self._key = key
        self._attr_name = f"{name} reset"

    @property
    def native_value(self) -> datetime | None:
        data = self.coordinator.data
        if not isinstance(data, SubscriptionUsage):
            return None
        window = data.windows.get(self._key)
        return window.resets_at if window else None


class ExtraUsageSensor(ClaudeEntity, SensorEntity):
    """Pay-as-you-go 'extra usage' on top of a subscription."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:cash-plus"
    _attr_name = "Extra usage"

    def __init__(self, coordinator: ClaudeUsageCoordinator) -> None:
        super().__init__(coordinator, "extra_usage")

    @property
    def _extra(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        return data.extra_usage if isinstance(data, SubscriptionUsage) else None

    @property
    def native_value(self) -> float | None:
        extra = self._extra or {}
        util = extra.get("utilization")
        if util is not None:
            return float(util)
        used, limit = extra.get("used_credits"), extra.get("monthly_limit")
        if used is not None and limit:
            return round(float(used) / float(limit) * 100, 1)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return dict(self._extra) if self._extra else None


# ------------------------------------------------------------------- admin API


@dataclass(frozen=True, kw_only=True)
class AdminSensorDescription(SensorEntityDescription):
    """Admin API sensor description."""

    value_fn: Callable[[AdminUsage], float | int | None]
    period: str  # "month" or "today"


def _token_desc(field: str | None, period: str) -> AdminSensorDescription:
    label = {"input": "Input", "output": "Output", "cache_read": "Cache read", "cache_write": "Cache write"}
    attr = "tokens_month" if period == "month" else "tokens_today"
    suffix = "this month" if period == "month" else "today"
    if field is None:
        return AdminSensorDescription(
            key=f"tokens_total_{period}",
            name=f"Total tokens {suffix}",
            icon="mdi:counter",
            native_unit_of_measurement="tokens",
            state_class=SensorStateClass.TOTAL,
            value_fn=lambda d, a=attr: sum(getattr(d, a).values()),
            period=period,
        )
    return AdminSensorDescription(
        key=f"tokens_{field}_{period}",
        name=f"{label[field]} tokens {suffix}",
        icon="mdi:counter",
        native_unit_of_measurement="tokens",
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda d, a=attr, f=field: getattr(d, a)[f],
        period=period,
        entity_registry_enabled_default=field in ("input", "output"),
    )


ADMIN_SENSORS: tuple[AdminSensorDescription, ...] = (
    *(_token_desc(f, p) for p in ("month", "today") for f in (None, *TOKEN_FIELDS)),
    AdminSensorDescription(
        key="cost_month",
        name="Cost this month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.cost_month,
        period="month",
    ),
    AdminSensorDescription(
        key="cost_today",
        name="Cost today",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.cost_today,
        period="today",
    ),
)


class AdminSensor(ClaudeEntity, SensorEntity):
    """Anthropic Admin API usage sensor."""

    entity_description: AdminSensorDescription

    def __init__(
        self, coordinator: ClaudeUsageCoordinator, description: AdminSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def _data(self) -> AdminUsage | None:
        data = self.coordinator.data
        return data if isinstance(data, AdminUsage) else None

    @property
    def native_value(self) -> float | int | None:
        data = self._data
        return self.entity_description.value_fn(data) if data else None

    @property
    def last_reset(self) -> datetime | None:
        data = self._data
        if not data:
            return None
        return data.month_start if self.entity_description.period == "month" else data.day_start

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self._data
        if data and self.entity_description.key == "tokens_total_month":
            return {"by_model": data.by_model_month}
        return None
