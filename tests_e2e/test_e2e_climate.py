"""End-to-end: over_climate VTherm (bathroom) + the real inhibition plugin."""

from __future__ import annotations

import pytest

from . import SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

# pylint: disable=wrong-import-position
from custom_components.versatile_thermostat.const import (
    HVAC_OFF_REASON_INHIBITION,
    HVAC_OFF_REASON_MANUAL,
    HVAC_OFF_REASON_WINDOW_DETECTION,
)
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from custom_components.versatile_thermostat.vtherm_preset import VThermPreset
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .harness import (
    BATHROOM_BYPASS,
    BATHROOM_SENSOR,
    SOURCE,
    UNDERLYING_CLIMATE,
    WINDOW,
    Scenario,
    Underlyings,
    plugin_entity,
    send_temperature,
)


def assert_running(sc: Scenario) -> None:
    """VTherm regulates normally: heat, no inhibition withdrawal."""
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_HEAT
    assert vt.hvac_off_reason is None
    assert sc.attrs["withdrawal_active"] is False


def assert_withdrawn(sc: Scenario, provider_status: str = "active", reason: str = HVAC_OFF_REASON_INHIBITION) -> None:
    """VTherm control withdrawn by the inhibition."""
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == reason
    attrs = sc.attrs
    assert attrs["withdrawal_active"] is True
    assert attrs["provider_status"] == provider_status
    assert attrs["entry_handled"] is True


async def test_bathroom_turn_off(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Activation delay, one stop, preset change while withdrawn, one reconciliation."""
    sc = await setup_bathroom(activation=10, deactivation=30, policy="turn_off")
    assert_running(sc)
    assert sc.attrs["provider_status"] == "inactive"
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF
    climate = underlyings.climates[UNDERLYING_CLIMATE]
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(9)
    assert_running(sc)
    assert underlyings.calls == []
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF

    await advance(2)
    assert_withdrawn(sc)
    assert sc.attrs["physical_stop_result"] == "sent"
    assert sc.attrs["effective_stop_policy"] == "turn_off"
    assert underlyings.hvac_modes() == ["off"]
    assert underlyings.temperatures() == []
    assert hass.states.get(UNDERLYING_CLIMATE).state == "off"
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_ON
    assert hass.states.get(BATHROOM_SENSOR).attributes["provider_attached"] is True

    # Preset change while withdrawn: retained, nothing sent
    underlyings.reset()
    await sc.vt.async_set_preset_mode(VThermPreset.ECO)
    await hass.async_block_till_done()
    assert sc.vt.requested_state.preset == VThermPreset.ECO
    assert_withdrawn(sc)
    for _ in range(3):
        await send_temperature(hass, sc.vt, 14)
        await advance(10)
    assert underlyings.calls == []

    # Source off: nothing before the deactivation delay
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(29)
    assert_withdrawn(sc)
    assert underlyings.calls == []

    await advance(2)
    assert_running(sc)
    assert sc.vt.preset_mode == VThermPreset.ECO
    assert sc.vt.target_temperature == 17
    assert underlyings.hvac_modes() == ["heat"]
    assert underlyings.temperatures()[-1] == 17
    assert 21 not in underlyings.temperatures()
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"
    assert climate.target_temperature == 17
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF
    # The withdrawal guard is reset for the next activation
    assert sc.attrs["physical_stop_emitted"] is False

    # Nothing more later (one reconciliation only)
    await advance(10)
    assert underlyings.hvac_modes() == ["heat"]


async def test_bathroom_do_not_turn_off(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """No stop sent; a direct change on the underlying is not overwritten."""
    sc = await setup_bathroom(activation=10, deactivation=30, policy="do_not_turn_off")
    climate = underlyings.climates[UNDERLYING_CLIMATE]
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc)
    assert sc.attrs["effective_stop_policy"] == "do_not_turn_off"
    assert sc.attrs["physical_stop_result"] == "not_required"
    assert sc.attrs["physical_stop_emitted"] is False
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
    assert_withdrawn(sc)
    assert sc.vt.requested_state.hvac_mode == VThermHvacMode_HEAT

    # Exit: VTherm takes the device back with its own setpoint
    await send_temperature(hass, sc.vt, 14)
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    assert_running(sc)
    assert underlyings.temperatures()[-1] == 21
    assert climate.target_temperature == 21


async def test_unavailable_grace(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Short unavailability ignored; longer -> UNKNOWN -> withdrawal; INACTIVE confirmed -> resume."""
    sc = await setup_bathroom(activation=10, deactivation=30, grace=60)
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await advance(59)
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(120)
    assert_running(sc)
    assert underlyings.calls == []
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_OFF

    await send_temperature(hass, sc.vt)
    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await advance(59)
    assert_running(sc)
    assert underlyings.calls == []
    await advance(2)
    assert_withdrawn(sc, provider_status="unknown")
    assert sc.attrs["awaiting_confirmation"] is True
    assert underlyings.hvac_modes() == ["off"]
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_UNKNOWN

    underlyings.reset()
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(29)
    assert_withdrawn(sc, provider_status="unknown")
    assert underlyings.calls == []
    await advance(2)
    assert_running(sc)
    assert sc.attrs["provider_status"] == "inactive"
    assert underlyings.hvac_modes() == ["heat"]


async def test_unavailable_then_active_no_second_stop(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance
) -> None:
    """UNKNOWN withdrawal followed by a confirmed ACTIVE: still one single stop."""
    sc = await setup_bathroom(activation=10, deactivation=30, grace=0)
    underlyings.reset()
    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert_withdrawn(sc, provider_status="unknown")
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc, provider_status="active")
    assert underlyings.hvac_modes() == ["off"]


async def test_bypass(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Bypass on during an inhibition resumes at once; off inhibits again after the delay."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc)
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
    assert_withdrawn(sc)
    assert underlyings.hvac_modes() == ["off"]


async def test_manual_off_during_inhibition(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """A manual OFF requested while withdrawn is kept after the exit."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc)
    underlyings.reset()

    await sc.vt.async_set_hvac_mode(VThermHvacMode_OFF)
    await hass.async_block_till_done()
    assert_withdrawn(sc)
    assert underlyings.calls == []

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_MANUAL
    assert sc.attrs["withdrawal_active"] is False
    assert "heat" not in underlyings.hvac_modes()
    assert hass.states.get(UNDERLYING_CLIMATE).state == "off"


async def test_window_and_inhibition(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Both active -> reason inhibition; inhibition ends with the window open -> off for the window."""
    sc = await setup_bathroom(window=True, activation=10, deactivation=30)
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc)
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(WINDOW, STATE_ON)
    await advance(1)
    assert sc.vt.window_state == STATE_ON
    assert_withdrawn(sc)
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    vt = sc.vt
    assert sc.attrs["withdrawal_active"] is False
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_WINDOW_DETECTION
    assert "heat" not in underlyings.hvac_modes()

    await send_temperature(hass, sc.vt)
    hass.states.async_set(WINDOW, STATE_OFF)
    await advance(1)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["off", "heat"]


async def test_window_first_then_inhibition(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """Window open first, then inhibition: reason inhibition, no extra command; window closes while
    inhibited: still withdrawn."""
    sc = await setup_bathroom(window=True, activation=10, deactivation=30)
    underlyings.reset()

    hass.states.async_set(WINDOW, STATE_ON)
    await advance(1)
    assert sc.vt.hvac_off_reason == HVAC_OFF_REASON_WINDOW_DETECTION
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc)
    assert underlyings.hvac_modes() == ["off"]

    hass.states.async_set(WINDOW, STATE_OFF)
    await advance(1)
    assert_withdrawn(sc)
    assert underlyings.hvac_modes() == ["off"]
