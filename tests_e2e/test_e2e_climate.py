"""End-to-end: over_climate VTherm (bathroom) + the real inhibition plugin."""

from __future__ import annotations

import pytest

from . import SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

# pylint: disable=wrong-import-position
from unittest.mock import patch

from custom_components.versatile_thermostat.const import (
    HVAC_OFF_REASON_INHIBITION,
    HVAC_OFF_REASON_MANUAL,
    HVAC_OFF_REASON_WINDOW_DETECTION,
)
from custom_components.versatile_thermostat.underlyings import UnderlyingClimate
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.versatile_thermostat.vtherm_preset import VThermPreset
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .harness import (
    BATHROOM_BYPASS,
    BATHROOM_SENSOR,
    CLIMATE_VT_ID,
    SOURCE,
    UNDERLYING_CLIMATE,
    WINDOW,
    FacadeCalls,
    Scenario,
    Underlyings,
    plugin_entity,
    send_temperature,
)


def assert_running(sc: Scenario) -> None:
    """VTherm regulates normally: heat, gate open."""
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_HEAT
    assert vt.hvac_off_reason is None
    assert vt.is_inhibited is False
    assert sc.attrs is None or sc.attrs["command_gate"] == "OPEN"


def assert_inhibited(sc: Scenario, stop_requested: bool = True, reason: str = HVAC_OFF_REASON_INHIBITION) -> None:
    """VTherm control withdrawn by the inhibition."""
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == reason
    attrs = sc.attrs
    assert attrs["inhibited"] is True
    assert attrs["command_gate"] == "CLOSED"
    assert attrs["entry_stop_requested"] is stop_requested


async def test_bathroom_stop_underlyings(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """Activation delay, one set and one stop, preset change while inhibited, one unset and reconciliation."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    assert_running(sc)
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF
    # Setup re-asserts the confirmed INACTIVE: a no-op in the core
    assert facade_calls.of(CLIMATE_VT_ID) == [("unset", None)]
    climate = underlyings.climates[UNDERLYING_CLIMATE]
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(9)
    assert_running(sc)
    assert underlyings.calls == []
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF

    await advance(2)
    assert_inhibited(sc)
    assert sc.attrs["entry_stop_completed"] is True
    assert facade_calls.of(CLIMATE_VT_ID)[1:] == [("set", True)]
    assert underlyings.hvac_modes() == ["off"]
    assert underlyings.temperatures() == []
    assert hass.states.get(UNDERLYING_CLIMATE).state == "off"
    sensor = hass.states.get(BATHROOM_SENSOR)
    assert sensor.state == STATE_ON
    assert sensor.attributes["last_api_call"] == "set"
    assert sensor.attributes["stop_underlyings"] is True
    assert sensor.attributes["active_sources"] == [SOURCE]

    # Preset change while inhibited: retained, nothing sent
    underlyings.reset()
    await sc.vt.async_set_preset_mode(VThermPreset.ECO)
    await hass.async_block_till_done()
    assert sc.vt.requested_state.preset == VThermPreset.ECO
    assert_inhibited(sc)
    for _ in range(3):
        await send_temperature(hass, sc.vt, 14)
        await advance(10)
    assert underlyings.calls == []

    # Source off: nothing before the deactivation delay
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(29)
    assert_inhibited(sc)
    assert underlyings.calls == []

    await advance(2)
    assert_running(sc)
    assert facade_calls.of(CLIMATE_VT_ID)[1:] == [("set", True), ("unset", None)]
    assert sc.vt.preset_mode == VThermPreset.ECO
    assert sc.vt.target_temperature == 17
    assert underlyings.hvac_modes() == ["heat"]
    assert underlyings.temperatures()[-1] == 17
    assert 21 not in underlyings.temperatures()
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"
    assert climate.target_temperature == 17
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF
    assert sc.attrs["inhibited"] is False
    assert sc.attrs["entry_stop_requested"] is False

    # Nothing more later (one reconciliation only)
    await advance(10)
    assert underlyings.hvac_modes() == ["heat"]
    assert len(facade_calls.of(CLIMATE_VT_ID)) == 3


async def test_bathroom_without_stop(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """stop_underlyings false: no stop sent; a direct change on the underlying is not overwritten."""
    sc = await setup_bathroom(activation=10, deactivation=30, stop_underlyings=False)
    climate = underlyings.climates[UNDERLYING_CLIMATE]
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc, stop_requested=False)
    assert facade_calls.of(CLIMATE_VT_ID)[-1] == ("set", False)
    assert sc.attrs["entry_stop_completed"] is False
    assert underlyings.calls == []
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"

    # Someone drives the radiator directly
    climate.set_temperature(temperature=26)
    climate.set_hvac_mode("heat")
    await hass.async_block_till_done()
    for _ in range(4):  # several control cycles (cycle_min is 5 min)
        await send_temperature(hass, sc.vt, 14)
        await sc.vt.async_control_heating(force=True)
        await advance(299)
    assert underlyings.calls == []
    assert climate.target_temperature == 26
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"
    assert_inhibited(sc, stop_requested=False)
    assert sc.vt.requested_state.hvac_mode == VThermHvacMode_HEAT

    # Exit: VTherm takes the device back with its own setpoint
    await send_temperature(hass, sc.vt, 14)
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    assert_running(sc)
    assert underlyings.temperatures()[-1] == 21
    assert climate.target_temperature == 21


async def test_unavailable_grace(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """Short unavailability ignored; longer -> UNKNOWN -> set; INACTIVE confirmed -> unset."""
    sc = await setup_bathroom(activation=10, deactivation=30, grace=60)
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await advance(59)
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(120)
    assert_running(sc)
    assert underlyings.calls == []
    assert facade_calls.of(CLIMATE_VT_ID) == [("unset", None)]

    await send_temperature(hass, sc.vt)
    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await advance(59)
    assert_running(sc)
    assert underlyings.calls == []
    await advance(2)
    assert_inhibited(sc)
    assert facade_calls.of(CLIMATE_VT_ID)[-1] == ("set", True)
    assert underlyings.hvac_modes() == ["off"]
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_UNKNOWN

    underlyings.reset()
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(29)
    assert_inhibited(sc)
    assert underlyings.calls == []
    await advance(2)
    assert_running(sc)
    assert facade_calls.of(CLIMATE_VT_ID)[-1] == ("unset", None)
    assert underlyings.hvac_modes() == ["heat"]


async def test_unknown_then_active_no_second_stop(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """UNKNOWN followed by a confirmed ACTIVE: one set, one single stop."""
    sc = await setup_bathroom(activation=10, deactivation=30, grace=0)
    underlyings.reset()
    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert_inhibited(sc)
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)
    assert underlyings.hvac_modes() == ["off"]
    assert facade_calls.of(CLIMATE_VT_ID) == [("unset", None), ("set", True)]


async def test_bypass(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """Bypass on during an inhibition releases at once; off inhibits again after the delay."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)
    underlyings.reset()

    bypass = plugin_entity(hass, BATHROOM_BYPASS)
    await bypass.async_turn_on()
    await hass.async_block_till_done()
    assert hass.states.get(BATHROOM_BYPASS).state == STATE_ON
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF
    await advance(5)  # delayed temperature resend after the restart of the climate

    underlyings.reset()
    await send_temperature(hass, sc.vt)
    await bypass.async_turn_off()
    await advance(9)
    assert_running(sc)
    assert underlyings.calls == []
    await advance(2)
    assert_inhibited(sc)
    assert underlyings.hvac_modes() == ["off"]
    assert [name for name, _ in facade_calls.of(CLIMATE_VT_ID)] == ["unset", "set", "unset", "set"]


async def test_manual_off_during_inhibition(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """A manual OFF requested while inhibited is kept after the exit."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)
    underlyings.reset()

    await sc.vt.async_set_hvac_mode(VThermHvacMode_OFF)
    await hass.async_block_till_done()
    assert_inhibited(sc)
    assert underlyings.calls == []

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_MANUAL
    assert sc.attrs["inhibited"] is False
    assert "heat" not in underlyings.hvac_modes()
    assert hass.states.get(UNDERLYING_CLIMATE).state == "off"


async def test_window_and_inhibition(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Both active -> reason inhibition; inhibition ends with the window open -> off for the window."""
    sc = await setup_bathroom(window=True, activation=10, deactivation=30)
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(WINDOW, STATE_ON)
    await advance(1)
    assert sc.vt.window_state == STATE_ON
    assert_inhibited(sc)
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    vt = sc.vt
    assert sc.attrs["inhibited"] is False
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_WINDOW_DETECTION
    assert "heat" not in underlyings.hvac_modes()

    await send_temperature(hass, sc.vt)
    hass.states.async_set(WINDOW, STATE_OFF)
    await advance(1)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["off", "heat"]


async def test_window_first_then_inhibition(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Window open first, then inhibition: reason inhibition; window closes while inhibited: still inhibited."""
    sc = await setup_bathroom(window=True, activation=10, deactivation=30)
    underlyings.reset()

    hass.states.async_set(WINDOW, STATE_ON)
    await advance(1)
    assert sc.vt.hvac_off_reason == HVAC_OFF_REASON_WINDOW_DETECTION
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)
    assert underlyings.hvac_modes() == ["off", "off"]  # the entry stop is unconditional

    hass.states.async_set(WINDOW, STATE_OFF)
    await advance(1)
    assert_inhibited(sc)
    assert underlyings.hvac_modes() == ["off", "off"]


async def test_failed_exit_is_retried(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """The core fails its exit reconciliation (stays inhibited): the plugin retries 30 s later."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)
    underlyings.reset()

    original = UnderlyingClimate.async_inhibition_resync
    failures = [RuntimeError("resync failed")]

    async def _resync(self) -> None:
        if failures:
            raise failures.pop()
        await original(self)

    with patch.object(UnderlyingClimate, "async_inhibition_resync", _resync):
        hass.states.async_set(SOURCE, STATE_OFF)
        await advance(31)
        assert_inhibited(sc)
        assert sc.attrs["last_error_code"] == "exit_reconcile_failed"
        sensor = hass.states.get(BATHROOM_SENSOR).attributes
        assert sensor["status"] == "inactive"
        assert sensor["last_error"] == "RuntimeError: resync failed"
        assert sensor["retry_pending"] is True
        assert "heat" not in underlyings.hvac_modes()

        await send_temperature(hass, sc.vt)
        await advance(30)
    assert_running(sc)
    assert underlyings.hvac_modes()[-1] == "heat"
    assert [name for name, _ in facade_calls.of(CLIMATE_VT_ID)] == ["unset", "set", "unset", "unset"]
    sensor = hass.states.get(BATHROOM_SENSOR).attributes
    assert sensor["last_error"] is None
    assert sensor["retry_pending"] is False
