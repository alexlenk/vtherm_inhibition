"""Configuration model, policy normalization and aggregation rules."""

from __future__ import annotations

import pytest
from homeassistant.core import State
from vtherm_api import InhibitionStatus, InhibitionStopPolicy

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_CLIMATE_HVAC_MODES,
    CONF_DEACTIVATION_DELAY,
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
    DEFAULT_CLIMATE_HVAC_MODES,
    MAX_DELAY_SECONDS,
)
from custom_components.vtherm_inhibition.detector import aggregate, source_contribution
from custom_components.vtherm_inhibition.models import InhibitionConfig, normalize_policy

MODES = frozenset(DEFAULT_CLIMATE_HVAC_MODES)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("turn_off", InhibitionStopPolicy.TURN_OFF),
        ("do_not_turn_off", InhibitionStopPolicy.DO_NOT_TURN_OFF),
        (" DO_NOT_TURN_OFF ", InhibitionStopPolicy.DO_NOT_TURN_OFF),
        (InhibitionStopPolicy.DO_NOT_TURN_OFF, InhibitionStopPolicy.DO_NOT_TURN_OFF),
        ("keep_on", InhibitionStopPolicy.TURN_OFF),
        (None, InhibitionStopPolicy.TURN_OFF),
        ("", InhibitionStopPolicy.TURN_OFF),
        (42, InhibitionStopPolicy.TURN_OFF),
    ],
)
def test_normalize_policy(value, expected) -> None:
    """Invalid or missing policies fall back to TURN_OFF."""
    assert normalize_policy(value) is expected


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
    assert config.physical_stop_policy is InhibitionStopPolicy.TURN_OFF


def test_config_options_override_data() -> None:
    """Options win over data; sources are deduplicated; delays clamped."""
    config = InhibitionConfig.from_entry(
        {
            CONF_VTHERM_UNIQUE_ID: "uid",
            CONF_SOURCE_ENTITIES: ["switch.a"],
            CONF_PHYSICAL_STOP_POLICY: "turn_off",
        },
        {
            CONF_SOURCE_ENTITIES: ["switch.b", "climate.ac", "switch.b"],
            CONF_CLIMATE_HVAC_MODES: ["cool"],
            CONF_ACTIVATION_DELAY: 12.7,
            CONF_DEACTIVATION_DELAY: -5,
            CONF_UNAVAILABLE_GRACE_PERIOD: MAX_DELAY_SECONDS * 2,
            CONF_PHYSICAL_STOP_POLICY: "do_not_turn_off",
        },
    )
    assert config.source_entities == ("switch.b", "climate.ac")
    assert config.climate_hvac_modes == frozenset({"cool"})
    assert config.activation_delay == 12
    assert config.deactivation_delay == 0
    assert config.unavailable_grace_period == MAX_DELAY_SECONDS
    assert config.physical_stop_policy is InhibitionStopPolicy.DO_NOT_TURN_OFF


def test_config_invalid_values() -> None:
    """Garbage delays use defaults, an invalid policy falls back to TURN_OFF."""
    config = InhibitionConfig.from_entry(
        {
            CONF_SOURCE_ENTITIES: "switch.single",
            CONF_ACTIVATION_DELAY: "abc",
            CONF_PHYSICAL_STOP_POLICY: "explode",
        }
    )
    assert config.source_entities == ("switch.single",)
    assert config.activation_delay == 30
    assert config.physical_stop_policy is InhibitionStopPolicy.TURN_OFF


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
