"""Configuration model, migration of version 1 settings and aggregation rules."""

from __future__ import annotations

import pytest
from homeassistant.core import State

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_CLIMATE_HVAC_MODES,
    CONF_DEACTIVATION_DELAY,
    CONF_LEGACY_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_STOP_UNDERLYINGS,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
    DEFAULT_CLIMATE_HVAC_MODES,
    MAX_DELAY_SECONDS,
)
from custom_components.vtherm_inhibition.detector import aggregate, source_contribution
from custom_components.vtherm_inhibition.models import (
    ApiCall,
    InhibitionConfig,
    InhibitionStatus,
    call_for,
    migrate_v1_settings,
)

MODES = frozenset(DEFAULT_CLIMATE_HVAC_MODES)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("turn_off", True),
        ("do_not_turn_off", False),
        (" DO_NOT_TURN_OFF ", False),
        ("keep_on", True),
        (None, True),
        ("", True),
        (42, True),
    ],
)
def test_migrate_v1_policy(value, expected) -> None:
    """Version 1 policy -> stop_underlyings; invalid or missing values were TURN_OFF."""
    migrated = migrate_v1_settings({CONF_SOURCE_ENTITIES: ["switch.a"], CONF_LEGACY_PHYSICAL_STOP_POLICY: value})
    assert migrated == {CONF_SOURCE_ENTITIES: ["switch.a"], CONF_STOP_UNDERLYINGS: expected}


def test_migrate_v1_without_policy() -> None:
    """Settings without the old key are left unchanged (no stop_underlyings added)."""
    assert migrate_v1_settings({CONF_SOURCE_ENTITIES: ["switch.a"]}) == {CONF_SOURCE_ENTITIES: ["switch.a"]}
    assert migrate_v1_settings({}) == {}


def test_call_for_status() -> None:
    """ACTIVE and UNKNOWN keep the VTherm inhibited; only INACTIVE releases it."""
    assert call_for(InhibitionStatus.ACTIVE) is ApiCall.SET
    assert call_for(InhibitionStatus.UNKNOWN) is ApiCall.SET
    assert call_for(InhibitionStatus.INACTIVE) is ApiCall.UNSET


def test_config_defaults() -> None:
    """Missing options take their documented defaults."""
    config = InhibitionConfig.from_entry({CONF_VTHERM_UNIQUE_ID: "uid", CONF_VTHERM_ENTITY_ID: "climate.x"})
    assert config.vtherm_unique_id == "uid"
    assert config.vtherm_entity_id == "climate.x"
    assert config.source_entities == ()
    assert config.climate_hvac_modes == frozenset({"heat", "cool", "heat_cool", "auto", "dry"})
    assert config.activation_delay == 30
    assert config.deactivation_delay == 60
    assert config.unavailable_grace_period == 300
    assert config.stop_underlyings is True


def test_config_options_override_data() -> None:
    """Options win over data; sources are deduplicated; delays clamped."""
    config = InhibitionConfig.from_entry(
        {
            CONF_VTHERM_UNIQUE_ID: "uid",
            CONF_SOURCE_ENTITIES: ["switch.a"],
            CONF_STOP_UNDERLYINGS: True,
        },
        {
            CONF_SOURCE_ENTITIES: ["switch.b", "climate.ac", "switch.b"],
            CONF_CLIMATE_HVAC_MODES: ["cool"],
            CONF_ACTIVATION_DELAY: 12.7,
            CONF_DEACTIVATION_DELAY: -5,
            CONF_UNAVAILABLE_GRACE_PERIOD: MAX_DELAY_SECONDS * 2,
            CONF_STOP_UNDERLYINGS: False,
        },
    )
    assert config.source_entities == ("switch.b", "climate.ac")
    assert config.climate_hvac_modes == frozenset({"cool"})
    assert config.activation_delay == 12
    assert config.deactivation_delay == 0
    assert config.unavailable_grace_period == MAX_DELAY_SECONDS
    assert config.stop_underlyings is False


def test_config_invalid_values() -> None:
    """Garbage delays use defaults, a non-boolean stop_underlyings falls back to True."""
    config = InhibitionConfig.from_entry(
        {
            CONF_SOURCE_ENTITIES: "switch.single",
            CONF_ACTIVATION_DELAY: "abc",
            CONF_STOP_UNDERLYINGS: "no",
        }
    )
    assert config.source_entities == ("switch.single",)
    assert config.activation_delay == 30
    assert config.stop_underlyings is True


@pytest.mark.parametrize(
    ("entity_id", "state", "expected"),
    [
        ("binary_sensor.x", "on", "active"),
        ("binary_sensor.x", "off", "inactive"),
        ("input_boolean.x", "on", "active"),
        ("switch.x", "off", "inactive"),
        ("switch.x", "unavailable", "unavailable"),
        ("switch.x", "unknown", "unavailable"),
        ("binary_sensor.x", "weird", "unavailable"),
        ("climate.x", "heat", "active"),
        ("climate.x", "cool", "active"),
        ("climate.x", "heat_cool", "active"),
        ("climate.x", "auto", "active"),
        ("climate.x", "dry", "active"),
        ("climate.x", "fan_only", "inactive"),
        ("climate.x", "off", "inactive"),
        ("climate.x", "unavailable", "unavailable"),
    ],
)
def test_source_contribution(entity_id: str, state: str, expected: str) -> None:
    """Per-domain interpretation of a source state."""
    assert source_contribution(State(entity_id, state), MODES) == expected


def test_source_contribution_missing_entity() -> None:
    """A source that does not exist is unavailable."""
    assert source_contribution(None, MODES) == "unavailable"


def test_source_contribution_custom_modes() -> None:
    """Only the configured modes inhibit."""
    assert source_contribution(State("climate.ac", "cool"), frozenset({"heat"})) == "inactive"
    assert source_contribution(State("climate.ac", "heat"), frozenset({"heat"})) == "active"


@pytest.mark.parametrize(
    ("contributions", "expected"),
    [
        (["active"], InhibitionStatus.ACTIVE),
        (["inactive", "active"], InhibitionStatus.ACTIVE),
        (["unavailable", "active"], InhibitionStatus.ACTIVE),
        (["inactive", "unavailable"], InhibitionStatus.UNKNOWN),
        (["unavailable"], InhibitionStatus.UNKNOWN),
        (["inactive", "inactive"], InhibitionStatus.INACTIVE),
        ([], InhibitionStatus.INACTIVE),
    ],
)
def test_aggregate(contributions: list[str], expected: InhibitionStatus) -> None:
    """Three-valued OR."""
    assert aggregate(contributions) is expected
