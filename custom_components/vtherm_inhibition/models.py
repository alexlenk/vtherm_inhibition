"""Statuses and configuration model of one VTherm inhibition entry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .const import (
    CONF_ACTIVATION_DELAY,
    CONF_CLIMATE_HVAC_MODES,
    CONF_DEACTIVATION_DELAY,
    CONF_LEGACY_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_STOP_UNDERLYINGS,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
    DEFAULT_ACTIVATION_DELAY,
    DEFAULT_CLIMATE_HVAC_MODES,
    DEFAULT_DEACTIVATION_DELAY,
    DEFAULT_STOP_UNDERLYINGS,
    DEFAULT_UNAVAILABLE_GRACE_PERIOD,
    LEGACY_POLICY_DO_NOT_TURN_OFF,
    MAX_DELAY_SECONDS,
)


class InhibitionStatus(StrEnum):
    """Confirmed (or raw) inhibition status, owned by the plugin."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class ApiCall(StrEnum):
    """The two operations of the vtherm_api inhibition facade."""

    SET = "set"
    UNSET = "unset"


def call_for(status: InhibitionStatus) -> ApiCall:
    """``ACTIVE`` and ``UNKNOWN`` keep the VTherm inhibited; only ``INACTIVE`` releases it."""
    return ApiCall.UNSET if status is InhibitionStatus.INACTIVE else ApiCall.SET


#: Keys editable through the options flow.
OPTION_KEYS = (
    CONF_SOURCE_ENTITIES,
    CONF_CLIMATE_HVAC_MODES,
    CONF_ACTIVATION_DELAY,
    CONF_DEACTIVATION_DELAY,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_STOP_UNDERLYINGS,
)


def default_options() -> dict[str, Any]:
    """Return the default values of the editable options."""
    return {
        CONF_SOURCE_ENTITIES: [],
        CONF_CLIMATE_HVAC_MODES: list(DEFAULT_CLIMATE_HVAC_MODES),
        CONF_ACTIVATION_DELAY: DEFAULT_ACTIVATION_DELAY,
        CONF_DEACTIVATION_DELAY: DEFAULT_DEACTIVATION_DELAY,
        CONF_UNAVAILABLE_GRACE_PERIOD: DEFAULT_UNAVAILABLE_GRACE_PERIOD,
        CONF_STOP_UNDERLYINGS: DEFAULT_STOP_UNDERLYINGS,
    }


def migrate_v1_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Replace the version 1 ``physical_stop_policy`` by ``stop_underlyings``.

    ``do_not_turn_off`` becomes ``False``; ``turn_off`` and any missing or
    invalid value (treated as ``turn_off`` by version 1) become ``True``.
    Settings without the old key are returned unchanged.
    """
    migrated = dict(settings)
    if CONF_LEGACY_PHYSICAL_STOP_POLICY in migrated:
        policy = str(migrated.pop(CONF_LEGACY_PHYSICAL_STOP_POLICY) or "").strip().lower()
        migrated[CONF_STOP_UNDERLYINGS] = policy != LEGACY_POLICY_DO_NOT_TURN_OFF
    return migrated


def _seconds(value: Any, default: int) -> int:
    """Return a delay in seconds clamped to [0, MAX_DELAY_SECONDS]."""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_DELAY_SECONDS, seconds))


@dataclass(frozen=True, slots=True)
class InhibitionConfig:
    """Effective configuration of the inhibition of one VTherm."""

    vtherm_unique_id: str
    vtherm_entity_id: str
    source_entities: tuple[str, ...]
    climate_hvac_modes: frozenset[str]
    activation_delay: int
    deactivation_delay: int
    unavailable_grace_period: int
    stop_underlyings: bool

    @classmethod
    def from_entry(cls, data: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> InhibitionConfig:
        """Build the config from entry data overridden by entry options."""
        merged: dict[str, Any] = default_options()
        merged.update(data)
        merged.update(options or {})
        sources = merged.get(CONF_SOURCE_ENTITIES) or []
        if isinstance(sources, str):
            sources = [sources]
        # Keep the configured order but drop duplicates.
        unique_sources = tuple(dict.fromkeys(str(s) for s in sources if s))
        modes = merged.get(CONF_CLIMATE_HVAC_MODES)
        if modes is None:
            modes = DEFAULT_CLIMATE_HVAC_MODES
        stop_underlyings = merged.get(CONF_STOP_UNDERLYINGS)
        return cls(
            vtherm_unique_id=str(merged.get(CONF_VTHERM_UNIQUE_ID) or ""),
            vtherm_entity_id=str(merged.get(CONF_VTHERM_ENTITY_ID) or ""),
            source_entities=unique_sources,
            climate_hvac_modes=frozenset(str(m) for m in modes),
            activation_delay=_seconds(merged.get(CONF_ACTIVATION_DELAY), DEFAULT_ACTIVATION_DELAY),
            deactivation_delay=_seconds(merged.get(CONF_DEACTIVATION_DELAY), DEFAULT_DEACTIVATION_DELAY),
            unavailable_grace_period=_seconds(
                merged.get(CONF_UNAVAILABLE_GRACE_PERIOD), DEFAULT_UNAVAILABLE_GRACE_PERIOD
            ),
            stop_underlyings=stop_underlyings if isinstance(stop_underlyings, bool) else DEFAULT_STOP_UNDERLYINGS,
        )
