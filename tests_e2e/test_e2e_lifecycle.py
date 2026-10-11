"""End-to-end: plugin/VTherm lifecycle (discovery order, reloads, unload, delete, restart)."""

from __future__ import annotations

import pytest

from . import SKIP_REASON

if SKIP_REASON:
    pytest.skip(SKIP_REASON, allow_module_level=True)

# pylint: disable=wrong-import-position
from typing import ClassVar
from unittest.mock import patch

from custom_components.versatile_thermostat.const import HVAC_OFF_REASON_INHIBITION
from custom_components.versatile_thermostat.vtherm_central_api import VersatileThermostatAPI
from custom_components.versatile_thermostat.vtherm_hvac_mode import VThermHvacMode_HEAT, VThermHvacMode_OFF
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    PROVIDER_NAME,
)
from custom_components.vtherm_inhibition.factory import InhibitionProviderFactory
from custom_components.vtherm_inhibition.provider import VThermInhibitionProvider

from .harness import (
    BATHROOM_BYPASS,
    BATHROOM_SENSOR,
    CLIMATE_VT_ID,
    SOURCE,
    UNDERLYING_CLIMATE,
    Scenario,
    Underlyings,
    climate_vt_entry,
    heat_comfort,
    inhibition_attrs,
    plugin_entry,
    setup_entry,
    vtherm,
)


def _api(hass: HomeAssistant) -> VersatileThermostatAPI:
    return VersatileThermostatAPI.get_vtherm_api(hass)


def assert_single_attachment(sc: Scenario) -> None:
    """Exactly one live provider, the one the VTherm holds."""
    providers = sc.detector.providers
    assert len(providers) == 1
    assert sc.vt.inhibition_manager.provider is providers[0]
    assert providers[0].is_listening
    assert isinstance(_api(sc.hass).get_inhibition_provider_factory(PROVIDER_NAME), InhibitionProviderFactory)
    assert sc.vt.inhibition_manager.conflicts == []
    assert sc.attrs["rejected_providers"] == []


def assert_running(sc: Scenario) -> None:
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_HEAT
    assert vt.hvac_off_reason is None


def assert_withdrawn(sc: Scenario, provider_status: str = "active") -> None:
    vt = sc.vt
    assert vt.vtherm_hvac_mode == VThermHvacMode_OFF
    assert vt.hvac_off_reason == HVAC_OFF_REASON_INHIBITION
    assert sc.attrs["withdrawal_active"] is True
    assert sc.attrs["provider_status"] == provider_status


# ---------------------------------------------------------------------------
# Plugin added while Home Assistant runs
# ---------------------------------------------------------------------------
async def test_add_entry_while_running_source_off(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings) -> None:
    """A heating VTherm gets the provider without any command (no spurious stop)."""
    calls_before = list(underlyings.calls)
    sc = await setup_bathroom(activation=10, deactivation=30)
    added = underlyings.calls[len(calls_before) :]
    assert "off" not in [str(data.get("hvac_mode")) for srv, _, data in added if srv == "set_hvac_mode"]
    assert_running(sc)
    assert sc.attrs["provider_status"] == "inactive"
    assert sc.attrs["physical_stop_emitted"] is False
    assert_single_attachment(sc)
    assert hass.states.get(UNDERLYING_CLIMATE).state == "heat"


async def test_add_entry_while_running_source_on(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings) -> None:
    """Source already active: first status confirmed at once, one single stop."""
    hass.states.async_set(SOURCE, STATE_ON)
    calls_before = len(underlyings.calls)
    sc = await setup_bathroom(activation=10, deactivation=30)
    added = underlyings.calls[calls_before:]
    assert [str(d["hvac_mode"]) for srv, _, d in added if srv == "set_hvac_mode"].count("off") == 1
    assert_withdrawn(sc)
    assert sc.attrs["physical_stop_result"] == "sent"
    assert_single_attachment(sc)


async def test_second_entry_for_another_vtherm(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings) -> None:
    """With the factory already registered, a new entry still reaches its VTherm (reload path)."""
    from .harness import SWITCH_VT_ENTRY_ID, SWITCH_VT_ID, switch_vt_entry

    sc = await setup_bathroom(activation=10, deactivation=30)
    await setup_entry(hass, switch_vt_entry())
    office = vtherm(hass, SWITCH_VT_ID)
    assert inhibition_attrs(hass, SWITCH_VT_ID) is None  # ABSENT
    hass.states.async_set("input_boolean.office_heater", STATE_ON)
    office_entry = plugin_entry(
        vtherm_entry_id=SWITCH_VT_ENTRY_ID, vtherm_entity_id=SWITCH_VT_ID, sources=["input_boolean.office_heater"]
    )
    await setup_entry(hass, office_entry)
    office = vtherm(hass, SWITCH_VT_ID)
    assert office.inhibition_manager.provider is not None
    assert inhibition_attrs(hass, SWITCH_VT_ID)["provider_status"] == "active"
    assert office.hvac_off_reason == HVAC_OFF_REASON_INHIBITION
    assert len(office_entry.runtime_data.providers) == 1
    # The bathroom is untouched
    assert_running(sc)
    assert_single_attachment(sc)


# ---------------------------------------------------------------------------
# Startup: the core discovers the factory itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("plugin_first", [True, False])
@pytest.mark.parametrize("source_state", [STATE_OFF, STATE_ON])
async def test_startup_discovery(
    hass: HomeAssistant, underlyings: Underlyings, advance, plugin_first: bool, source_state: str
) -> None:
    """Plugin configured, then HA restarts: entries set up while HA starts (both orders),
    no VTherm reload, the core attaches the provider in async_startup."""
    vt_entry = climate_vt_entry()
    await setup_entry(hass, vt_entry)
    underlyings.install()
    await heat_comfort(hass, vtherm(hass, CLIMATE_VT_ID))
    await advance(5)
    p_entry = plugin_entry(activation=10, deactivation=30)
    p_entry.add_to_hass(hass)  # configured, set up at the next start
    sc = Scenario(hass, vt_entry, p_entry, CLIMATE_VT_ID)
    hass.states.async_set(SOURCE, source_state)

    reloads = []
    original_reload = hass.config_entries.async_reload

    async def _spy_reload(entry_id):
        reloads.append(entry_id)
        return await original_reload(entry_id)

    with patch.object(hass.config_entries, "async_reload", _spy_reload):
        await _restart(hass, sc, underlyings, plugin_first)
    assert reloads == []
    assert sc.vt.is_ready
    assert_single_attachment(sc)
    if source_state == STATE_OFF:
        assert_running(sc)
        assert "off" not in underlyings.hvac_modes()
    else:
        assert_withdrawn(sc)
        assert underlyings.hvac_modes() == ["off"]


class RecordingProvider(VThermInhibitionProvider):
    """The real provider, recording the calls made by the core."""

    calls: ClassVar[list[str]] = []

    def post_init(self, entry_infos):
        self.calls.append("post_init")
        super().post_init(entry_infos)

    def restore_state(self, old_state):
        self.calls.append("restore_state" if old_state is not None else "restore_state(None)")
        super().restore_state(old_state)

    def register_transition_callback(self, callback_):
        self.calls.append("register_transition_callback")
        return super().register_transition_callback(callback_)

    async def async_start_listening(self):
        self.calls.append("async_start_listening")
        await super().async_start_listening()

    async def async_refresh_state(self):
        self.calls.append("async_refresh_state")
        return await super().async_refresh_state()

    def stop_listening(self):
        self.calls.append("stop_listening")
        super().stop_listening()


@pytest.mark.parametrize("source_state", [STATE_OFF, STATE_ON])
async def test_core_call_order(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, source_state) -> None:
    """The core order (create, post_init, restore_state, register, start, refresh) works with
    the plugin (its restore_state is a no-op); the first refresh gives the right status."""
    RecordingProvider.calls = []
    with patch("custom_components.vtherm_inhibition.factory.VThermInhibitionProvider", RecordingProvider):
        sc = await setup_bathroom(activation=10, deactivation=30)
        # Plugin added while running: late attachment, no previous state given to restore
        assert RecordingProvider.calls == [
            "post_init",
            "register_transition_callback",
            "async_start_listening",
            "async_refresh_state",
        ]
        # the source changes while Home Assistant is down
        sc.detector._unsub_sources()  # pylint: disable=protected-access
        sc.detector._unsub_sources = None  # pylint: disable=protected-access
        hass.states.async_set(SOURCE, source_state)
        RecordingProvider.calls = []
        await _restart(hass, sc, underlyings, plugin_first=True)

    assert RecordingProvider.calls == [
        "stop_listening",  # unload of the plugin entry
        "post_init",
        "restore_state",  # previous VTherm state, startup only
        "register_transition_callback",
        "async_start_listening",
        "async_refresh_state",
    ]
    assert_single_attachment(sc)
    if source_state == STATE_OFF:
        assert_running(sc)
    else:
        assert_withdrawn(sc)


# ---------------------------------------------------------------------------
# VTherm reload
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("inhibited", [False, True])
async def test_vtherm_reload_keeps_provider(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, inhibited: bool
) -> None:
    """A VTherm reload keeps the VThermAPI, the factory, and reattaches; no second stop."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    factory = _api(hass).get_inhibition_provider_factory(PROVIDER_NAME)
    if inhibited:
        hass.states.async_set(SOURCE, STATE_ON)
        await advance(11)
        assert_withdrawn(sc)
    old_vt = sc.vt
    underlyings.reset()

    assert await hass.config_entries.async_reload(sc.vt_entry.entry_id)
    await hass.async_block_till_done()
    await advance(5)

    assert sc.vt is not old_vt
    # The core rebuilds its API when its last entry is unloaded: the factory is carried over
    assert isinstance(_api(hass), VersatileThermostatAPI)
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is factory
    assert_single_attachment(sc)
    if inhibited:
        assert_withdrawn(sc)
        assert underlyings.calls == []
        assert sc.attrs["physical_stop_emitted"] is True
    else:
        assert_running(sc)
        assert "off" not in underlyings.hvac_modes()

    # The new instance still follows the provider
    hass.states.async_set(SOURCE, STATE_OFF if inhibited else STATE_ON)
    await advance(31)
    if inhibited:
        assert_running(sc)
    else:
        assert_withdrawn(sc)


async def test_plugin_unload_after_the_last_vtherm(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings) -> None:
    """The last VTherm unloaded, then the plugin: the shared API stays a core API, so the
    VTherm can be set up again and gets its provider back when the plugin returns."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    assert await hass.config_entries.async_unload(sc.vt_entry.entry_id)
    assert await hass.config_entries.async_unload(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    assert isinstance(_api(hass), VersatileThermostatAPI)

    assert await hass.config_entries.async_setup(sc.vt_entry.entry_id)
    await hass.async_block_till_done()
    assert sc.vt_entry.state is ConfigEntryState.LOADED
    assert sc.attrs is None
    assert await hass.config_entries.async_setup(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    assert_single_attachment(sc)
    assert sc.attrs["provider_status"] == "inactive"


# ---------------------------------------------------------------------------
# Plugin entry unload / disable / delete / options change
# ---------------------------------------------------------------------------
async def _inhibit(hass: HomeAssistant, sc: Scenario, advance) -> None:
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(11)
    assert_withdrawn(sc)


@pytest.mark.parametrize("inhibited", [False, True])
async def test_unload_entry(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, inhibited) -> None:
    """Plain unload: UNKNOWN then unregistered; the VTherm stays safely withdrawn."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    old_provider = sc.vt.inhibition_manager.provider

    assert await hass.config_entries.async_unload(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    await advance(60)

    assert_withdrawn(sc, provider_status="unknown")
    assert sc.attrs["last_error_code"] == "provider_unregistered"
    assert sc.vt.inhibition_manager.provider is None
    assert old_provider.is_listening is False
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is None
    # precautionary stop only if the VTherm was not withdrawn already
    assert underlyings.hvac_modes() == ([] if inhibited else ["off"])

    # A source change has no effect any more (provider detached)
    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(60)
    assert_withdrawn(sc, provider_status="unknown")

    # Setting it up again resumes (source off)
    underlyings.reset()
    assert await hass.config_entries.async_setup(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    assert_running(sc)
    assert_single_attachment(sc)
    assert underlyings.hvac_modes() == ["heat"]


@pytest.mark.parametrize("inhibited", [False, True])
async def test_disable_entry(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, inhibited) -> None:
    """Disable: the VTherm is reloaded without provider (ABSENT) and works as before."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()

    assert await hass.config_entries.async_set_disabled_by(sc.plugin_entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    await advance(5)

    assert sc.attrs is None  # ABSENT: no inhibition block
    assert sc.vt.inhibition_manager.is_attached is False
    assert_running(sc)
    # Not inhibited: the provider reports UNKNOWN on unload (contract), so the core sends
    # its precautionary stop before the VTherm is reloaded without provider and resumes.
    assert underlyings.hvac_modes() == (["heat"] if inhibited else ["off", "heat"])


@pytest.mark.parametrize("inhibited", [False, True])
async def test_delete_entry(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, inhibited) -> None:
    """Delete: the VTherm is reloaded without provider (ABSENT) and resumes."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()

    await hass.config_entries.async_remove(sc.plugin_entry.entry_id)
    await hass.async_block_till_done()
    await advance(5)

    assert sc.attrs is None
    assert sc.vt.inhibition_manager.is_attached is False
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is None
    assert_running(sc)
    assert underlyings.hvac_modes() == (["heat"] if inhibited else ["off", "heat"])
    assert hass.states.get(BATHROOM_SENSOR) is None

    # The source has no effect any more
    hass.states.async_set(SOURCE, STATE_ON)
    await advance(60)
    assert_running(sc)


@pytest.mark.parametrize("inhibited", [False, True])
async def test_options_change(hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, inhibited) -> None:
    """Options change: applied in place, same provider, no command to the underlyings."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    if inhibited:
        await _inhibit(hass, sc, advance)
    underlyings.reset()
    old_provider = sc.vt.inhibition_manager.provider
    sequence = old_provider.sequence

    hass.config_entries.async_update_entry(
        sc.plugin_entry, options={CONF_ACTIVATION_DELAY: 3, CONF_PHYSICAL_STOP_POLICY: "do_not_turn_off"}
    )
    await hass.async_block_till_done()
    await advance(60)

    assert sc.plugin_entry.state is ConfigEntryState.LOADED
    assert sc.detector.config.activation_delay == 3
    assert sc.vt.inhibition_manager.provider is old_provider
    assert old_provider.sequence == sequence  # no transition at all
    assert_single_attachment(sc)
    assert underlyings.calls == []
    if inhibited:
        assert_withdrawn(sc)
        # The policy captured at entry is kept until the exit
        assert sc.attrs["effective_stop_policy"] == "turn_off"
    else:
        assert_running(sc)
        # The new delay and policy apply to the next entry: no turn_off
        hass.states.async_set(SOURCE, STATE_ON)
        await advance(4)
        assert_withdrawn(sc)
        assert sc.attrs["effective_stop_policy"] == "do_not_turn_off"
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
    assert_withdrawn(sc)
    assert underlyings.calls == []
    await advance(1)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]
    assert hass.states.get(BATHROOM_SENSOR).attributes["sources"] == {
        other: {"state": "off", "contribution": "inactive"}
    }


# ---------------------------------------------------------------------------
# Duplicate transitions (refresh + callback)
# ---------------------------------------------------------------------------
async def test_refresh_and_callback_idempotent(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance
) -> None:
    """The same status delivered by the callback and by async_refresh_state: one stop, one exit."""
    sc = await setup_bathroom(activation=0, deactivation=0)
    manager = sc.vt.inhibition_manager
    underlyings.reset()

    # callback first, then refresh
    hass.states.async_set(SOURCE, STATE_ON)
    await hass.async_block_till_done()
    await manager.async_refresh()
    await manager.async_refresh()
    await hass.async_block_till_done()
    assert_withdrawn(sc)
    assert underlyings.hvac_modes() == ["off"]

    # refresh first (missed event), the provider then notifies the same transition
    detector = sc.detector
    detector._unsub_sources()  # pylint: disable=protected-access
    detector._unsub_sources = None  # pylint: disable=protected-access
    hass.states.async_set(SOURCE, STATE_OFF)
    await hass.async_block_till_done()
    assert_withdrawn(sc)
    await manager.async_refresh()  # the detector confirms INACTIVE inside the refresh
    await hass.async_block_till_done()
    await manager.async_refresh()
    await hass.async_block_till_done()
    await advance(5)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["off", "heat"]
    assert sc.attrs["provider_status"] == "inactive"


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
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    # The core's own EVENT_HOMEASSISTANT_STARTED listener was registered at the first
    # (running) setup of the integration: run its body like Home Assistant would.
    await _api(hass).init_vtherm_links()
    await hass.async_block_till_done()


@pytest.mark.parametrize("plugin_first", [True, False])
async def test_restart_while_inhibited(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, plugin_first: bool
) -> None:
    """Restart during an active inhibition: still withdrawn, no second stop; then resumes."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    await _inhibit(hass, sc, advance)
    assert sc.attrs["physical_stop_emitted"] is True

    await _restart(hass, sc, underlyings, plugin_first)

    assert sc.vt.is_ready
    assert_withdrawn(sc)
    assert sc.attrs["physical_stop_emitted"] is True
    assert underlyings.calls == []
    assert_single_attachment(sc)

    hass.states.async_set(SOURCE, STATE_OFF)
    await advance(31)
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]


@pytest.mark.parametrize("plugin_first", [True, False])
async def test_restart_source_off(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance, plugin_first: bool
) -> None:
    """Restart with the inhibition ended during the downtime: resumes at startup, once."""
    sc = await setup_bathroom(activation=10, deactivation=30)
    await _inhibit(hass, sc, advance)
    # the source goes off while HA is down (no event seen by the plugin)
    detector = sc.detector
    detector._unsub_sources()  # pylint: disable=protected-access
    detector._unsub_sources = None  # pylint: disable=protected-access
    hass.states.async_set(SOURCE, STATE_OFF)

    await _restart(hass, sc, underlyings, plugin_first)

    assert sc.vt.is_ready
    assert_running(sc)
    assert sc.attrs["provider_status"] == "inactive"
    assert underlyings.hvac_modes() == ["heat"]
    assert_single_attachment(sc)


async def test_restart_source_not_yet_available(
    hass: HomeAssistant, setup_bathroom, underlyings: Underlyings, advance
) -> None:
    """Source missing at startup: the restored ACTIVE is held for the grace period (no
    stop, no resume), the first live value is applied at once."""
    sc = await setup_bathroom(activation=10, deactivation=30, grace=300)
    await _inhibit(hass, sc, advance)
    hass.states.async_remove(SOURCE)

    await _restart(hass, sc, underlyings, plugin_first=True)
    assert_withdrawn(sc)
    assert underlyings.calls == []
    assert hass.states.get(BATHROOM_SENSOR).state == STATE_ON

    hass.states.async_set(SOURCE, STATE_OFF)
    await hass.async_block_till_done()
    assert_running(sc)
    assert underlyings.hvac_modes() == ["heat"]


# ---------------------------------------------------------------------------
# Sources feeding back into the plugin are refused (review item 1)
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
            await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_SOURCE_ENTITIES: [forbidden]}
            )
    assert sc.detector.config.source_entities == (SOURCE,)
