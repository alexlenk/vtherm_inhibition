"""Factory, entry lifecycle, VTherm (re)discovery and entities."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from vtherm_api import InhibitionStatus, VThermAPI

from custom_components.vtherm_inhibition.const import (
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    DATA_DETECTORS,
    DOMAIN,
    PROVIDER_NAME,
    VT_DOMAIN,
)
from custom_components.vtherm_inhibition.factory import InhibitionProviderFactory, async_register_factory

from .conftest import VT_UNIQUE_ID, FakeThermostat, plugin_entry

ACTIVE = InhibitionStatus.ACTIVE
INACTIVE = InhibitionStatus.INACTIVE
UNKNOWN = InhibitionStatus.UNKNOWN
HEATER = "input_boolean.heater"


def _api(hass: HomeAssistant) -> VThermAPI:
    return VThermAPI.get_vtherm_api(hass)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
async def test_factory_supports_and_create(hass: HomeAssistant, setup_plugin, thermostat) -> None:
    """Only VTherms with an entry are supported; matching uses unique_id."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    factory = InhibitionProviderFactory(hass)
    assert factory.name == PROVIDER_NAME
    assert factory.supports(thermostat) is True
    assert factory.create(thermostat).status is INACTIVE

    other = FakeThermostat(hass, "other-uid", "climate.bathroom")  # same entity_id, other VTherm
    assert factory.supports(other) is False
    with pytest.raises(ValueError):
        factory.create(other)

    # Without unique_id, fall back to the entity_id.
    assert factory.supports(FakeThermostat(hass, "", "climate.bathroom")) is True
    assert factory.supports(FakeThermostat(hass, "", "climate.kitchen")) is False

    await hass.config_entries.async_unload(entry.entry_id)
    assert factory.supports(thermostat) is False


async def test_factory_registered_once_and_unregistered_with_last_entry(hass: HomeAssistant, setup_plugin) -> None:
    """Registered on first setup, kept while one entry remains, removed with the last."""
    hass.states.async_set(HEATER, "off")
    first = await setup_plugin([HEATER])
    factory = _api(hass).get_inhibition_provider_factory(PROVIDER_NAME)
    assert factory is not None
    second = await setup_plugin([HEATER], unique_id="vt-kitchen-uid", entity_id="climate.kitchen")
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is factory
    assert _api(hass).list_inhibition_provider_factories() == [PROVIDER_NAME]

    assert await hass.config_entries.async_unload(first.entry_id)
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is factory
    assert await hass.config_entries.async_unload(second.entry_id)
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is None
    assert not hass.data[DOMAIN][DATA_DETECTORS]

    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is not None


async def test_stale_factory_replaced(hass: HomeAssistant) -> None:
    """A factory left by a previous load of this plugin is replaced."""
    stale = InhibitionProviderFactory(hass)
    _api(hass).register_inhibition_provider_factory(stale)
    assert async_register_factory(hass) is True
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is not stale


async def test_foreign_factory_conflict(hass: HomeAssistant, caplog) -> None:
    """Another plugin owning the name is not replaced: the conflict is logged."""

    class Foreign:
        name = PROVIDER_NAME

        def supports(self, thermostat):
            return False

        def create(self, thermostat):
            raise NotImplementedError

    foreign = Foreign()
    _api(hass).register_inhibition_provider_factory(foreign)
    assert async_register_factory(hass) is False
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is foreign
    assert "already registered" in caplog.text


# ---------------------------------------------------------------------------
# Lifecycle with a (fake) core
# ---------------------------------------------------------------------------
async def test_vtherm_before_plugin(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """VTherm loaded first (no provider), plugin then reloads it so it attaches."""
    assert fake_core.attach_count == 0
    assert VT_UNIQUE_ID not in fake_core.providers
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    assert fake_core.attach_count == 1
    assert fake_core.statuses[VT_UNIQUE_ID] is ACTIVE
    assert vtherm.state is ConfigEntryState.LOADED


async def test_core_attaching_on_registration(
    hass: HomeAssistant, vtherm, fake_core, thermostat, setup_plugin, monkeypatch
) -> None:
    """A core attaching the provider as soon as the factory is registered (the real
    core does) gets a started detector and its confirmed status, and the VTherm is
    not reloaded (no second attachment)."""
    statuses_at_registration: list[InhibitionStatus] = []
    original = VThermAPI.register_inhibition_provider_factory

    def _register_and_attach(api: VThermAPI, factory) -> None:
        original(api, factory)
        detector = hass.data[DOMAIN][DATA_DETECTORS][VT_UNIQUE_ID]
        statuses_at_registration.append(detector.status)
        hass.async_create_task(fake_core.attach(thermostat), eager_start=True)

    monkeypatch.setattr(VThermAPI, "register_inhibition_provider_factory", _register_and_attach)
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])

    assert statuses_at_registration == [INACTIVE]
    assert fake_core.attach_count == 1
    assert fake_core.statuses[VT_UNIQUE_ID] is INACTIVE
    assert fake_core.transitions == []
    assert len(entry.runtime_data.providers) == 1


async def test_plugin_before_vtherm_at_startup(hass: HomeAssistant, fake_core, thermostat, setup_plugin) -> None:
    """During startup the plugin does not reload anything; the core discovers it."""
    hass.set_state(CoreState.starting)
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    assert fake_core.attach_count == 0
    # Core async_startup after homeassistant_started:
    assert await fake_core.attach(thermostat) is INACTIVE
    hass.set_state(CoreState.running)


async def test_setup_while_starting_does_not_reload_vtherm(hass: HomeAssistant, vtherm, fake_core) -> None:
    """No VTherm reload while Home Assistant is not running."""
    hass.set_state(CoreState.starting)
    hass.states.async_set(HEATER, "off")
    entry = plugin_entry([HEATER])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert fake_core.attach_count == 0
    hass.set_state(CoreState.running)


async def test_unload_reports_unknown(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """Entry unload: the attached provider reports UNKNOWN to the core."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    provider = fake_core.providers[VT_UNIQUE_ID]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert [t.new_status for t in fake_core.transitions] == [UNKNOWN]
    assert provider.status is UNKNOWN
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is None
    # A plain unload keeps the VTherm withdrawn (no reload without provider).
    assert fake_core.attach_count == 1


async def test_options_change_in_place(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """Options change: no reload, same provider, no UNKNOWN; only a real
    change of the confirmed status is reported (with the normal delays)."""
    hass.states.async_set(HEATER, "on")
    hass.states.async_set("switch.ac_plug", "off")
    entry = await setup_plugin([HEATER])
    detector = entry.runtime_data
    provider = fake_core.providers[VT_UNIQUE_ID]
    sensor_unique = er.async_get(hass).async_get("binary_sensor.bathroom_inhibition").unique_id

    hass.config_entries.async_update_entry(
        entry,
        options={CONF_SOURCE_ENTITIES: ["switch.ac_plug"], CONF_PHYSICAL_STOP_POLICY: "do_not_turn_off"},
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is detector
    assert fake_core.providers[VT_UNIQUE_ID] is provider
    assert provider.is_listening
    assert fake_core.attach_count == 1
    # Delays 0 in this entry: the raw change ACTIVE -> INACTIVE is confirmed at once.
    assert [t.new_status for t in fake_core.transitions] == [INACTIVE]
    assert provider.physical_stop_policy == "do_not_turn_off"
    assert fake_core.transitions[0].stop_policy == "do_not_turn_off"
    # The old source is not followed any more, the new one is.
    hass.states.async_set(HEATER, "off")
    hass.states.async_set(HEATER, "on")
    await hass.async_block_till_done()
    assert len(fake_core.transitions) == 1
    hass.states.async_set("switch.ac_plug", "on")
    await hass.async_block_till_done()
    assert [t.new_status for t in fake_core.transitions] == [INACTIVE, ACTIVE]
    assert [t.sequence for t in fake_core.transitions] == [1, 2]
    # Entities follow the new configuration.
    state = hass.states.get("binary_sensor.bathroom_inhibition")
    assert er.async_get(hass).async_get("binary_sensor.bathroom_inhibition").unique_id == sensor_unique
    assert list(state.attributes["sources"]) == ["switch.ac_plug"]
    assert state.attributes["physical_stop_policy"] == "do_not_turn_off"
    assert _api(hass).get_inhibition_provider_factory(PROVIDER_NAME) is not None


async def test_options_policy_only_change_emits_nothing(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """A policy-only change is read live by the core at the next entry; no transition."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    provider = fake_core.providers[VT_UNIQUE_ID]
    hass.config_entries.async_update_entry(entry, options={CONF_PHYSICAL_STOP_POLICY: "do_not_turn_off"})
    await hass.async_block_till_done()
    assert fake_core.transitions == []
    assert provider.physical_stop_policy == "do_not_turn_off"
    assert await provider.async_refresh_state() is ACTIVE


async def test_options_unchanged_is_noop(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """Saving the same options (or a title change) does nothing."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    hass.config_entries.async_update_entry(entry, title="Bad", options={CONF_SOURCE_ENTITIES: [HEATER]})
    await hass.async_block_till_done()
    assert fake_core.transitions == []
    assert fake_core.attach_count == 1


async def test_remove_entry_releases_vtherm(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """Entry removal: the VTherm is reloaded and finds no provider any more."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert VT_UNIQUE_ID not in fake_core.providers
    assert UNKNOWN in [t.new_status for t in fake_core.transitions]


async def test_disable_entry_releases_vtherm(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """Disabling the entry reloads the VTherm without provider."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert VT_UNIQUE_ID not in fake_core.providers
    assert fake_core.attach_count == 1


async def test_end_to_end_transitions(hass: HomeAssistant, vtherm, fake_core, setup_plugin, advance) -> None:
    """Use case: an electric heater on PV surplus inhibits the bathroom radiator."""
    heater = "climate.bathroom_electric_heater_heater"
    hass.states.async_set(heater, "off")
    await setup_plugin([heater], activation=30, deactivation=60, grace=300)
    assert fake_core.statuses[VT_UNIQUE_ID] is INACTIVE

    hass.states.async_set(heater, "heat")
    await advance(30)
    hass.states.async_set(heater, "unavailable")  # HomeKit blip
    await advance(120)
    hass.states.async_set(heater, "heat")
    await advance(10)
    hass.states.async_set(heater, "off")
    await advance(60)
    assert [t.new_status for t in fake_core.transitions] == [ACTIVE, INACTIVE]
    assert [t.sequence for t in fake_core.transitions] == [1, 2]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
async def test_entities_on_vtherm_device(hass: HomeAssistant, vtherm, setup_plugin) -> None:
    """Entities are attached to the VTherm device, unique IDs derive from the VTherm."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    registry = er.async_get(hass)
    sensor = registry.async_get("binary_sensor.bathroom_inhibition")
    switch = registry.async_get("switch.bathroom_inhibition_bypass")
    assert sensor.unique_id == f"{VT_UNIQUE_ID}_inhibition"
    assert switch.unique_id == f"{VT_UNIQUE_ID}_inhibition_bypass"
    device = dr.async_get(hass).async_get_device_by_identifier((VT_DOMAIN, VT_UNIQUE_ID), vtherm.entry_id)
    assert sensor.device_id == device.id
    assert switch.device_id == device.id
    assert switch.entity_category == "config"


async def test_entities_own_device_without_vtherm(hass: HomeAssistant, setup_plugin) -> None:
    """Without a VTherm in the registry, entities get a plugin device."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    assert device is not None
    assert er.async_get(hass).async_get("binary_sensor.bathroom_inhibition").device_id == device.id


async def test_binary_sensor_states_and_attributes(hass: HomeAssistant, setup_plugin, advance) -> None:
    """on/off/unknown plus the diagnostic attributes."""
    ac = "climate.bathroom_ac"
    hass.states.async_set(HEATER, "off")
    hass.states.async_set(ac, "off")
    entry = await setup_plugin([HEATER, ac], activation=30, grace=60, physical_stop_policy="do_not_turn_off")
    state = hass.states.get("binary_sensor.bathroom_inhibition")
    assert state.state == "off"
    attrs = state.attributes
    assert attrs["status"] == "inactive"
    assert attrs["raw_status"] == "inactive"
    assert attrs["physical_stop_policy"] == "do_not_turn_off"
    assert attrs["bypass"] is False
    assert attrs["pending_status"] is None
    assert attrs["sources"] == {
        HEATER: {"state": "off", "contribution": "inactive"},
        ac: {"state": "off", "contribution": "inactive"},
    }
    assert attrs["activation_delay"] == 30
    assert attrs["unavailable_grace_period"] == 60
    assert attrs["provider_attached"] is False

    hass.states.async_set(ac, "cool")
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.bathroom_inhibition")
    assert state.state == "off"
    assert state.attributes["raw_status"] == "active"
    assert state.attributes["pending_status"] == "active"
    assert state.attributes["pending_until"] is not None
    assert state.attributes["sources"][ac] == {"state": "cool", "contribution": "active"}

    await advance(30)
    state = hass.states.get("binary_sensor.bathroom_inhibition")
    assert state.state == "on"
    assert state.attributes["status"] == "active"
    assert state.attributes["last_change"] is not None

    hass.states.async_set(ac, "unavailable")
    await advance(60)
    state = hass.states.get("binary_sensor.bathroom_inhibition")
    assert state.state == "unknown"
    assert state.attributes["status"] == "unknown"
    assert entry.runtime_data.status is UNKNOWN


async def test_provider_attached_attribute(hass: HomeAssistant, vtherm, setup_plugin) -> None:
    """The binary sensor shows whether the core attached a provider."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    assert hass.states.get("binary_sensor.bathroom_inhibition").attributes["provider_attached"] is True
