"""End-to-end: over_switch TPI VTherm (office) + the real inhibition plugin."""

from __future__ import annotations

import pytest

from . import SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

# pylint: disable=wrong-import-position
from unittest.mock import patch

from custom_components.versatile_thermostat.const import HVAC_OFF_REASON_INHIBITION
from custom_components.versatile_thermostat.cycle_scheduler import CycleScheduler
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from .harness import (
    OFFICE_SENSOR,
    SOURCE,
    SWITCH_VT_ENTRY_ID,
    SWITCH_VT_ID,
    UNDERLYING_SWITCH,
    FacadeCalls,
    Underlyings,
    heat_comfort,
    inhibition_attrs,
    plugin_entry,
    send_temperature,
    setup_entry,
    switch_vt_entry,
    vtherm,
)


async def test_over_switch_tpi(
    hass: HomeAssistant, underlyings: Underlyings, facade_calls: FacadeCalls, advance, caplog
) -> None:
    """One set and one switch stop on entry, no TPI cycle while inhibited, one unset and TPI resumes on exit."""
    await setup_entry(hass, switch_vt_entry())
    underlyings.install()
    await heat_comfort(hass, vtherm(hass, SWITCH_VT_ID))
    await setup_entry(
        hass,
        plugin_entry(vtherm_entry_id=SWITCH_VT_ENTRY_ID, vtherm_entity_id=SWITCH_VT_ID, activation=10, deactivation=30),
    )
    underlyings.install()
    switch = underlyings.switches[UNDERLYING_SWITCH]
    vt = vtherm(hass, SWITCH_VT_ID)
    await send_temperature(hass, vt)
    await vt.async_control_heating(force=True)
    await hass.async_block_till_done()
    assert vt.vtherm_hvac_mode == VThermHvacMode_HEAT
    assert vt.proportional_algorithm.on_percent > 0
    assert switch.is_on
    assert inhibition_attrs(hass, SWITCH_VT_ID) is None  # unset outside an inhibition: no-op, never used
    assert facade_calls.of(SWITCH_VT_ID) == [("unset", None)]
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    vt = vtherm(hass, SWITCH_VT_ID)
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_INHIBITION
    assert hass.states.get(OFFICE_SENSOR).state == STATE_ON
    assert len(underlyings.of("turn_off", UNDERLYING_SWITCH)) == 1
    assert underlyings.of("turn_on", UNDERLYING_SWITCH) == []
    assert not switch.is_on
    attrs = inhibition_attrs(hass, SWITCH_VT_ID)
    assert attrs["inhibited"] is True
    assert attrs["command_gate"] == "CLOSED"
    assert attrs["entry_stop_completed"] is True
    assert facade_calls.of(SWITCH_VT_ID) == [("unset", None), ("set", True)]
    # The reason is in the event log, under the VTherm's name (as the core writes its own events)
    assert f"{vt.name} - ---------------------> NEW EVENT: Inhibition requested by" in caplog.text
    assert f"(active source: {SOURCE})" in caplog.text

    # Several cycles: no TPI cycle, no command (even a forced control cycle)
    underlyings.reset()
    assert not vt.cycle_scheduler.is_cycle_running
    with patch.object(CycleScheduler, "_start_cycle_switch", autospec=True) as start_switch_cycle:
        for _ in range(4):
            await send_temperature(hass, vt, 14)
            await vt.async_control_heating(force=True)
            await advance(299)
    assert underlyings.calls == []
    assert not switch.is_on
    assert start_switch_cycle.call_count == 0
    assert not vt.cycle_scheduler.is_cycle_running
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF

    # Exit: TPI regulation resumes (switch turned on again by the cycle)
    await send_temperature(hass, vt)
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    assert vt.vtherm_hvac_mode == VThermHvacMode_HEAT
    assert vt.hvac_off_reason is None
    assert hass.states.get(OFFICE_SENSOR).state == STATE_OFF
    assert len(underlyings.of("turn_on", UNDERLYING_SWITCH)) >= 1
    assert switch.is_on
    assert inhibition_attrs(hass, SWITCH_VT_ID)["command_gate"] == "OPEN"
    assert facade_calls.of(SWITCH_VT_ID) == [("unset", None), ("set", True), ("unset", None)]


async def test_over_switch_without_stop(
    hass: HomeAssistant, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """stop_underlyings false: the switch is left on (e.g. driven by another automation), never touched."""
    await setup_entry(hass, switch_vt_entry())
    underlyings.install()
    await heat_comfort(hass, vtherm(hass, SWITCH_VT_ID))
    await setup_entry(
        hass,
        plugin_entry(
            vtherm_entry_id=SWITCH_VT_ENTRY_ID,
            vtherm_entity_id=SWITCH_VT_ID,
            activation=0,
            deactivation=0,
            stop_underlyings=False,
        ),
    )
    underlyings.install()
    switch = underlyings.switches[UNDERLYING_SWITCH]
    vt = vtherm(hass, SWITCH_VT_ID)
    await send_temperature(hass, vt)
    await vt.async_control_heating(force=True)
    await hass.async_block_till_done()
    assert switch.is_on
    underlyings.reset()

    hass.states.async_set(SOURCE, STATE_ON)
    await hass.async_block_till_done()
    assert vt.hvac_off_reason == HVAC_OFF_REASON_INHIBITION
    assert facade_calls.of(SWITCH_VT_ID)[-1] == ("set", False)
    for _ in range(3):
        await send_temperature(hass, vt, 14)
        await vt.async_control_heating(force=True)
        await advance(299)
    assert underlyings.calls == []
    assert switch.is_on
