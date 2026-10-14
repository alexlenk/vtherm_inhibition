"""End-to-end: plugin/VTherm lifecycle (add, restart, reloads, unload, disable, delete, options, rename)."""

from __future__ import annotations

import pytest

from . import SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

# pylint: disable=wrong-import-position
from custom_components.versatile_thermostat.const import HVAC_OFF_REASON_INHIBITION
from custom_components.versatile_thermostat.vtherm_central_api import VersatileThermostatAPI
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_SOURCE_ENTITIES,
    CONF_STOP_UNDERLYINGS,
    CONF_VTHERM_ENTITY_ID,
)

from .harness import (
    BATHROOM_BYPASS,
    BATHROOM_SENSOR,
    CLIMATE_VT_ID,
    SOURCE,
    UNDERLYING_CLIMATE,
    FacadeCalls,
    Scenario,
    Underlyings,
    climate_vt_entry,
    heat_comfort,
    inhibition_attrs,
    plugin_entry,
    setup_entry,
    vtherm,
)


def assert_running(sc: Scenario) -> None:
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_HEAT
    assert vt.hvac_off_reason is None
    assert vt.is_inhibited is False


def assert_inhibited(sc: Scenario) -> None:
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_INHIBITION
    assert sc.attrs["inhibited"] is True
    assert sc.attrs["command_gate"] == "CLOSED"


def names(facade_calls: FacadeCalls, entity_id: str = CLIMATE_VT_ID) -> list[str]:
    return [name for name, _ in facade_calls.of(entity_id)]


async def _inhibit(hass: HomeAssistant, sc: Scenario, advance) -> None:
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_inhibited(sc)


# ---------------------------------------------------------------------------
# Plugin added while Home Assistant runs
# ---------------------------------------------------------------------------
async def test_add_entry_while_running_source_off(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls
) -> None:
    """A heating VTherm: unset (a no-op in the core), no command at all."""
    calls_before = list(underlyings.calls)
    sc = await setup_bathroom(activation=10, deactivation=30)
    added = underlyings.calls[len(calls_before) :]
    assert "off" not in [str(data.get("hvac_mode")) for srv, _, data in added if srv == "set_hvac_mode"]
    assert_running(sc)
    assert sc.attrs is None  # inhibition never used: nothing published by the core
    assert names(facade_calls) == ["unset"]
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"


async def test_add_entry_while_running_source_on(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls
) -> None:
    """Source already active: first status confirmed at once, one set, one single stop."""
    hass.states.async_set(SOURCE, STATE_ON)
    calls_before = len(underlyings.calls)
    sc = await setup_bathroom(activation=10, deactivation=30)
    added = underlyings.calls[calls_before:]
    assert [str(d["hvac_mode"]) for srv, _, d in added if srv == "set_hvac_mode"].count("off") == 1
    assert_inhibited(sc)
    assert facade_calls.of(CLIMATE_VT_ID) == [("set", True)]


async def test_second_entry_for_another_vtherm(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls
) -> None:
    """Each entry drives its own VTherm only."""
    from .harness import SWITCH_VT_ENTRY_ID, SWITCH_VT_ID, switch_vt_entry

    sc = await setup_bathroom(activation=10, deactivation=30)
    await setup_entry(hass, switch_vt_entry())
    assert inhibition_attrs(hass, SWITCH_VT_ID) is None
    hass.states.async_set("input_boolean.office_heater", STATE_ON)
    office_entry = plugin_entry(
        vtherm_entry_id=SWITCH_VT_ENTRY_ID, vtherm_entity_id=SWITCH_VT_ID, sources=["input_boolean.office_heater"]
    )
    await setup_entry(hass, office_entry)
    office = vtherm(hass, SWITCH_VT_ID)
    assert inhibition_attrs(hass, SWITCH_VT_ID)["inhibited"] is True
    assert office.hvac_off_reason == HVAC_OFF_REASON_INHIBITION
    assert facade_calls.of(SWITCH_VT_ID) == [("set", True)]
    # The bathroom is untouched
    assert_running(sc)
    assert names(facade_calls) == ["unset"]


# ---------------------------------------------------------------------------
# Home Assistant restart
# ---------------------------------------------------------------------------
async def _restart(hass: HomeAssistant, sc: Scenario, underlyings: Underlyings, plugin_first: bool) -> None:
    """Simulate a restart: keep the last states, unload everything, set up again while
    Home Assistant starts, then start it (the core startup runs init_vtherm_links)."""
    saved = [
        hass.states.get(entity_id)
        for entity_id in (CLIMATE_VT_ID, BATHROOM_SENSOR, BATHROOM_BYPASS)
        if hass.states.get(entity_id) is not None
    ]
    for entry in (sc.plugin_entry, sc.vt_entry):
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_restore_cache(hass, saved)
    underlyings.reset()

    hass.set_state(CoreState.starting)
    for entry in (sc.plugin_entry, sc.vt_entry) if plugin_first else (sc.vt_entry, sc.plugin_entry):
        if entry.state is not ConfigEntryState.LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    # The core's own EVENT_HOMEASSISTANT_STARTED listener was registered at the first
    # (running) setup of the integration: run its body like Home Assistant would.
    await VersatileThermostatAPI.get_vtherm_api(hass).init_vtherm_links()
    await hass.async_block_till_done()


@pytest.mark.parametrize("plugin_first", [True, False])
@pytest.mark.parametrize("source_state", [STATE_OFF, STATE_ON])
async def test_startup(
    hass: HomeAssistant,
    underlyings: Underlyings,
    facade_calls: FacadeCalls,
    advance,
    plugin_first: bool,
    source_state: str,
) -> None:
    """Plugin configured, then HA restarts (both setup orders): the plugin calls the VTherm
    only once it has started (is_ready), never before the core restored its own state.

    The VTherm was not inhibited before the restart: it starts normally (its startup
    resends its state), then the plugin's set stops it once."""
    vt_entry = climate_vt_entry()
    await setup_entry(hass, vt_entry)
    underlyings.install()
    await heat_comfort(hass, vtherm(hass, CLIMATE_VT_ID))
    await advance(5)
    p_entry = plugin_entry(activation=10, deactivation=30)
    p_entry.add_to_hass(hass)  # configured, set up at the next start
    sc = Scenario(hass, vt_entry, p_entry, CLIMATE_VT_ID)
    hass.states.async_set(SOURCE, source_state)

    await _restart(hass, sc, underlyings, plugin_first)
    assert sc.vt.is_ready
    if source_state == STATE_OFF:
        assert_running(sc)
        assert "off" not in underlyings.hvac_modes()
        assert names(facade_calls) == ["unset"]
    else:
        assert_inhibited(sc)
        assert underlyings.hvac_modes() == ["heat", "off"]
        assert names(facade_calls) == ["set"]


@pytest.mark.parametrize("plugin_first", [True, False])
async def test_restart_while_inhibited(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, plugin_first: bool
) -> None:
    """Restart during an inhibition: the core restores it, the plugin re-asserts set after
    the restore: no second stop, no resume; then a confirmed INACTIVE resumes."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    await _inhibit(hass, sc, advance)
    assert sc.attrs["entry_stop_completed"] is True
    facade_calls.calls.clear()

    await _restart(hass, sc, underlyings, plugin_first)

    assert sc.vt.is_ready
    assert_inhibited(sc)
    assert sc.attrs["entry_stop_completed"] is True
    assert underlyings.calls == []
    assert facade_calls.of(CLIMATE_VT_ID) == [("set", True)]

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]
    assert names(facade_calls) == ["set", "unset"]


@pytest.mark.parametrize("plugin_first", [True, False])
async def test_restart_source_off(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, plugin_first: bool
) -> None:
    """The inhibition ended during the downtime: the first live value is confirmed at startup, one resume."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    await _inhibit(hass, sc, advance)
    # the source goes off while HA is down (no event seen by the plugin)
    sc.detector._unsubscribe()  # pylint: disable=protected-access
    hass.states.async_set(SOURCE, STATE_OFF)
    facade_calls.calls.clear()

    await _restart(hass, sc, underlyings, plugin_first)

    assert sc.vt.is_ready
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]
    assert names(facade_calls) == ["unset"]


async def test_restart_source_not_yet_available(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """Source missing at startup: the restored ACTIVE is held for the grace period and re-asserted
    (no stop, no resume, no unset); the first live value is applied at once."""
    sc = await setup_bathroom(activation=10, deactivation=30, grace=300)
    await _inhibit(hass, sc, advance)
    hass.states.async_remove(SOURCE)
    facade_calls.calls.clear()

    await _restart(hass, sc, underlyings, plugin_first=True)
    assert_inhibited(sc)
    assert underlyings.calls == []
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_ON
    assert names(facade_calls) == ["set"]

    hass.states.async_set(SOURCE, STATE_OFF)
    await hass.async_block_till_done()
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]
    assert names(facade_calls) == ["set", "unset"]


# ---------------------------------------------------------------------------
# VTherm reload
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("inhibited", [False, True])
async def test_vtherm_reload(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, inhibited: bool
) -> None:
    """A VTherm reload: the plugin re-asserts its call on the new instance; no second stop."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    old_vt = sc.vt
    underlyings.reset()
    facade_calls.calls.clear()

    assert await hass.config_entries.async_reload(sc.vt_entry.entry_id)
    await hass.async_block_till_done()
    await advance(5)

    assert sc.vt is not old_vt
    if inhibited:
        assert_inhibited(sc)
        assert underlyings.calls == []
        assert names(facade_calls) == ["set"]
    else:
        assert_running(sc)
        assert "off" not in underlyings.hvac_modes()
        assert names(facade_calls) == ["unset"]

    # The new instance follows the plugin
    hass.states.async_set(SOURCE, STATE_OFF if inhibited else STATE_ON)
    await advance(31)
    if inhibited:
        assert_running(sc)
    else:
        assert_inhibited(sc)


# ---------------------------------------------------------------------------
# Plugin entry reload / unload / disable / delete / options change
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("inhibited", [False, True])
async def test_reload_entry(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, inhibited: bool
) -> None:
    """Reloading the plugin entry: no unset while inhibited, the status is re-asserted, no command."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    facade_calls.calls.clear()

    assert await hass.config_entries.async_reload(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    await advance(60)

    assert names(facade_calls) == (["set"] if inhibited else ["unset"])
    assert underlyings.calls == []
    if inhibited:
        assert_inhibited(sc)
    else:
        assert_running(sc)


@pytest.mark.parametrize("inhibited", [False, True])
async def test_unload_entry(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, inhibited
) -> None:
    """Unload never implies INACTIVE: no call, the VTherm stays as it is; set up again, it follows."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    facade_calls.calls.clear()

    assert await hass.config_entries.async_unload(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    await advance(60)
    assert facade_calls.calls == []
    assert underlyings.calls == []
    if inhibited:
        assert_inhibited(sc)
        # A source change has no effect any more
        hass.states.async_set(SOURCE, STATE_OFF)
        await advance(60)
        assert_inhibited(sc)
    else:
        assert_running(sc)

    # Setting it up again: the first live value (source off) releases the VTherm
    hass.states.async_set(SOURCE, STATE_OFF)
    assert await hass.config_entries.async_setup(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    assert_running(sc)
    assert names(facade_calls) == ["unset"]
    assert underlyings.hvac_modes() == (["heat"] if inhibited else [])


@pytest.mark.parametrize("inhibited", [False, True])
async def test_disable_entry(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, inhibited
) -> None:
    """Disabling is an unload: an inhibited VTherm stays inhibited (no unset)."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    facade_calls.calls.clear()

    assert await hass.config_entries.async_set_disabled_by(sc.plugin_entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    await advance(60)

    assert facade_calls.calls == []
    assert underlyings.calls == []
    if inhibited:
        assert_inhibited(sc)
    else:
        assert_running(sc)


@pytest.mark.parametrize("inhibited", [False, True])
async def test_delete_entry(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, inhibited
) -> None:
    """Delete: unset releases the VTherm, which resumes; the source has no effect any more."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    facade_calls.calls.clear()

    await hass.config_entries.async_remove(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    await advance(5)

    assert names(facade_calls) == ["unset"]
    assert_running(sc)
    assert underlyings.hvac_modes() == (["heat"] if inhibited else [])
    assert hass.states.get(BATHROOM_SENSOR) is None

    hass.states.async_set(SOURCE, STATE_ON)
    await advance(60)
    assert_running(sc)


@pytest.mark.parametrize("inhibited", [False, True])
async def test_options_change(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance, inhibited
) -> None:
    """Options change: applied in place, no call, no command; stop_underlyings applies from the next entry."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    facade_calls.calls.clear()
    runtime = sc.plugin_entry.runtime_data

    hass.config_entries.async_update_entry(
        sc.plugin_entry, options={CONF_ACTIVATION_DELAY: 3, CONF_STOP_UNDERLYINGS: False}
    )
    await hass.async_block_till_done()
    await advance(60)

    assert sc.plugin_entry.state is ConfigEntryState.LOADED
    assert sc.plugin_entry.runtime_data is runtime
    assert sc.detector.config.activation_delay == 3
    assert facade_calls.calls == []
    assert underlyings.calls == []
    if inhibited:
        assert_inhibited(sc)
        assert sc.attrs["entry_stop_requested"] is True  # the current inhibition keeps its entry
    else:
        assert_running(sc)
        hass.states.async_set(SOURCE, STATE_ON)
        await advance(4)
        assert_inhibited(sc)
        assert sc.attrs["entry_stop_requested"] is False
        assert facade_calls.of(CLIMATE_VT_ID) == [("set", False)]
        assert underlyings.hvac_modes() == []


async def test_options_change_sources(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance) -> None:
    """New sources while inhibited: the exit goes through the deactivation delay, once."""
    other = "input_boolean.other_source"
    hass.states.async_set(other, STATE_OFF)
    sc = await setup_bathroom(activation=10, deactivation=30)
    await _inhibit(hass, sc, advance)
    underlyings.reset()

    hass.config_entries.async_update_entry(sc.plugin_entry, options={CONF_SOURCE_ENTITIES: [other]})
    await hass.async_block_till_done()
    await advance(29)
    assert_inhibited(sc)
    assert underlyings.calls == []
    await advance(1)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]
    assert hass.states.get(BATHROOM_SENSOR).attributes["sources"] == {
        other: {"state": "off", "contribution": "inactive"}
    }


# ---------------------------------------------------------------------------
# Renamed VTherm
# ---------------------------------------------------------------------------
async def test_vtherm_renamed_while_inhibited(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, facade_calls: FacadeCalls, advance
) -> None:
    """The VTherm entity ID is renamed while inhibited: the plugin follows it and releases it later."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    await _inhibit(hass, sc, advance)
    underlyings.reset()
    facade_calls.calls.clear()

    er.async_get(hass).async_update_entity(CLIMATE_VT_ID, new_entity_id="climate.bath")
    await hass.async_block_till_done()
    await advance(5)
    sc.entity_id = "climate.bath"
    assert sc.plugin_entry.data[CONF_VTHERM_ENTITY_ID] == "climate.bath"
    assert hass.states.get(BATHROOM_SENSOR).attributes["vtherm_entity_id"] == "climate.bath"
    assert_inhibited(sc)
    assert underlyings.calls == []

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    assert_running(sc)
    assert facade_calls.of("climate.bath")[-1] == ("unset", None)
    assert underlyings.hvac_modes() == ["heat"]


# ---------------------------------------------------------------------------
# Sources feeding back into the plugin are refused
# ---------------------------------------------------------------------------
async def test_options_refuse_feedback_sources(hass: HomeAssistant, setup_bathroom) -> None:
    """The VTherm's underlying climate and the plugin's own entities cannot be sources."""
    from homeassistant.data_entry_flow import InvalidData

    sc = await setup_bathroom(activation=10, deactivation=30)
    result = await hass.config_entries.options.async_init(sc.plugin_entry.entry_id)
    excluded = next(
        value.config["exclude_entities"]
        for key, value in result["data_schema"].schema.items()
        if str(key) == CONF_SOURCE_ENTITIES
    )
    for entity_id in (CLIMATE_VT_ID, UNDERLYING_CLIMATE, BATHROOM_SENSOR, BATHROOM_BYPASS):
        assert entity_id in excluded
    for forbidden in (UNDERLYING_CLIMATE, BATHROOM_SENSOR):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SOURCE_ENTITIES: [forbidden]})
    assert sc.detector.config.source_entities == (SOURCE,)
