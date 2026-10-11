"""Configuration model of one VTherm inhibition entry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vtherm_api import InhibitionStopPolicy
from vtherm_api.log_collector import get_vtherm_logger

from .const import (
    CONF_ACTIVATION_DELAY,
    CONF_CLIMATE_HVAC_MODES,
    CONF_DEACTIVATION_DELAY,
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
    DEFAULT_ACTIVATION_DELAY,
    DEFAULT_CLIMATE_HVAC_MODES,
    DEFAULT_DEACTIVATION_DELAY,
    DEFAULT_PHYSICAL_STOP_POLICY,
    DEFAULT_UNAVAILABLE_GRACE_PERIOD,
    MAX_DELAY_SECONDS,
)

_LOGGER = get_vtherm_logger(__name__)

#: Keys editable through the options flow.
OPTION_KEYS = (
    CONF_SOURCE_ENTITIES,
    CONF_CLIMATE_HVAC_MODES,
    CONF_ACTIVATION_DELAY,
    CONF_DEACTIVATION_DELAY,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_PHYSICAL_STOP_POLICY,
)


def default_options() -> dict[str, Any]:
    """Return the default values of the editable options."""
    return {
        CONF_SOURCE_ENTITIES: [],
        CONF_CLIMATE_HVAC_MODES: list(DEFAULT_CLIMATE_HVAC_MODES),
        CONF_ACTIVATION_DELAY: DEFAULT_ACTIVATION_DELAY,
        CONF_DEACTIVATION_DELAY: DEFAULT_DEACTIVATION_DELAY,
        CONF_UNAVAILABLE_GRACE_PERIOD: DEFAULT_UNAVAILABLE_GRACE_PERIOD,
        CONF_PHYSICAL_STOP_POLICY: DEFAULT_PHYSICAL_STOP_POLICY,
    }


def normalize_policy(value: Any) -> InhibitionStopPolicy:
    """Return the effective stop policy; ``TURN_OFF`` when unset or invalid."""
    if isinstance(value, InhibitionStopPolicy):
        return value
    try:
        return InhibitionStopPolicy(str(value).strip().lower())
    except ValueError:
        if value not in (None, ""):
            _LOGGER.warning(
                "Invalid physical stop policy %r, falling back to %s",
                value,
                InhibitionStopPolicy.TURN_OFF,
            )
        return InhibitionStopPolicy.TURN_OFF


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
    physical_stop_policy: InhibitionStopPolicy

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
            physical_stop_policy=normalize_policy(merged.get(CONF_PHYSICAL_STOP_POLICY)),
        )
