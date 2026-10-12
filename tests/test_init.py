"""Factory, entry lifecycle, VTherm (re)discovery and entities."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from vtherm_api import InhibitionStatus, InhibitionThermostatView, VThermAPI

from custom_components.vtherm_inhibition.const import (
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    DATA_DETECTORS,
    DOMAIN,
    PROVIDER_NAME,
    VT_DOMAIN,
)
from custom_components.vtherm_inhibition.factory import InhibitionProviderFactory, async_register_factory

from .conftest import VT_UNIQUE_ID, plugin_entry

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
    assert factory.supports(InhibitionThermostatView.from_thermostat(thermostat)) is True
    assert factory.create(InhibitionThermostatView.from_thermostat(thermostat)).status is INACTIVE

    other = InhibitionThermostatView("climate.bathroom", "Other", "other-uid")  # same entity_id, other VTherm
    assert factory.supports(other) is False
    with pytest.raises(ValueError):
        factory.create(other)

    # Without unique_id, fall back to the entity_id.
    assert factory.supports(InhibitionThermostatView("climate.bathroom", "B", "")) is True
    assert factory.supports(InhibitionThermostatView("climate.kitchen", "K", "")) is False

    await hass.config_entries.async_unload(entry.entry_id)
    assert factory.supports(InhibitionThermostatView.from_thermostat(thermostat)) is False


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


async def test_core_attaching_later_on_registration(
    hass: HomeAssistant, vtherm, fake_core, thermostat, setup_plugin, monkeypatch
) -> None:
    """A notifying core whose attach task has not run yet when setup checks
    (it suspends first) must not get an additional VTherm reload."""
    original = VThermAPI.register_inhibition_provider_factory

    def _register_and_attach_later(api: VThermAPI, factory) -> None:
        original(api, factory)
        hass.async_create_task(fake_core.attach(thermostat), eager_start=False)

    monkeypatch.setattr(VThermAPI, "register_inhibition_provider_factory", _register_and_attach_later)
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])

    assert fake_core.attach_count == 1
    assert fake_core.statuses[VT_UNIQUE_ID] is ACTIVE
    assert len(entry.runtime_data.providers) == 1


async def test_second_entry_reloads_its_vtherm(hass: HomeAssistant, vtherm, fake_core, thermostat, setup_plugin, monkeypatch) -> None:
    """With a notifying core, an entry added while the factory is already
    registered (another entry) still reloads its own VTherm."""
    original = VThermAPI.register_inhibition_provider_factory
    calls: list[str] = []

    def _register(api: VThermAPI, factory) -> None:
        calls.append("register")
        original(api, factory)

    monkeypatch.setattr(VThermAPI, "register_inhibition_provider_factory", _register)
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], unique_id="vt-kitchen-uid", entity_id="climate.kitchen")
    assert calls == ["register"]
    assert fake_core.attach_count == 0  # no kitchen VTherm loaded, nothing attached

    await setup_plugin([HEATER])
    assert calls == ["register"]
    assert fake_core.attach_count == 1  # bathroom VTherm reloaded
    assert fake_core.statuses[VT_UNIQUE_ID] is INACTIVE


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


# ---------------------------------------------------------------------------
# Core without inhibition support / not ready (review item 2)
# ---------------------------------------------------------------------------
def _install_api(hass: HomeAssistant, api_class: type[VThermAPI] | None) -> None:
    from vtherm_api.const import DOMAIN as API_DOMAIN, VTHERM_API_NAME

    VThermAPI.reset_vtherm_api()
    hass.data.setdefault(API_DOMAIN, {}).pop(VTHERM_API_NAME, None)
    if api_class is not None:
        api_class.get_vtherm_api(hass)


async def test_core_not_ready_retries_without_creating_api(hass: HomeAssistant, vt_module) -> None:
    """No core API yet: setup is retried and no bare VThermAPI is created."""
    from vtherm_api.const import DOMAIN as API_DOMAIN, VTHERM_API_NAME

    _install_api(hass, None)
    hass.states.async_set(HEATER, "off")
    entry = plugin_entry([HEATER])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.data.get(API_DOMAIN, {}).get(VTHERM_API_NAME) is None


async def test_core_without_inhibition_support(hass: HomeAssistant, vtherm, fake_core, caplog) -> None:
    """A core that does not override the registration (no #2087 support):
    setup error, repair issue, no VTherm reload, no misleading log."""
    from homeassistant.helpers import issue_registry as ir

    class OldCoreAPI(VThermAPI):
        """Released core: subclasses vtherm_api but has no inhibition runtime."""

    _install_api(hass, OldCoreAPI)
    hass.states.async_set(HEATER, "off")
    entry = plugin_entry([HEATER])
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, "core_without_inhibition") is not None
    assert fake_core.attach_count == 0
    assert "Reloading VTherm" not in caplog.text

    # Once the core is updated, the setup succeeds and the issue disappears.
    from .conftest import FakeCoreAPI

    _install_api(hass, FakeCoreAPI)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "core_without_inhibition") is None
    assert fake_core.attach_count == 1


async def test_unregister_does_not_create_api(hass: HomeAssistant, setup_plugin) -> None:
    """Unloading after the core dropped its API creates no bare API."""
    from vtherm_api.const import DOMAIN as API_DOMAIN, VTHERM_API_NAME

    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    hass.data[API_DOMAIN].pop(VTHERM_API_NAME)
    VThermAPI._hass = None  # pylint: disable=protected-access
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.data.get(API_DOMAIN, {}).get(VTHERM_API_NAME) is None


def test_core_supports_inhibition() -> None:
    """Both registration methods must be overridden by the core."""
    from custom_components.vtherm_inhibition.factory import core_supports_inhibition

    from .conftest import FakeCoreAPI

    class HalfAPI(VThermAPI):
        def register_inhibition_provider_factory(self, factory) -> None:
            super().register_inhibition_provider_factory(factory)

    assert core_supports_inhibition(FakeCoreAPI()) is True
    assert core_supports_inhibition(VThermAPI()) is False
    assert core_supports_inhibition(HalfAPI()) is False
    assert core_supports_inhibition(object()) is False


# ---------------------------------------------------------------------------
# Renamed / missing sources (review item 4)
# ---------------------------------------------------------------------------
async def test_renamed_source_is_followed(hass: HomeAssistant, vtherm, fake_core, setup_plugin) -> None:
    """A renamed source is replaced in the options and tracked under its new ID."""
    registry = er.async_get(hass)
    registry.async_get_or_create("input_boolean", "input_boolean", "heater", suggested_object_id="heater")
    hass.states.async_set(HEATER, "off")
    hass.states.async_set("binary_sensor.other", "off")
    # With a grace period (default 300 s), the rename blip is invisible.
    entry = await setup_plugin([HEATER, "binary_sensor.other"], grace=60)
    provider = fake_core.providers[VT_UNIQUE_ID]

    registry.async_update_entity(HEATER, new_entity_id="input_boolean.bath_heater")
    hass.states.async_remove(HEATER)
    hass.states.async_set("input_boolean.bath_heater", "off")
    await hass.async_block_till_done()

    assert entry.options[CONF_SOURCE_ENTITIES] == ["input_boolean.bath_heater", "binary_sensor.other"]
    assert entry.runtime_data.config.source_entities == ("input_boolean.bath_heater", "binary_sensor.other")
    assert fake_core.providers[VT_UNIQUE_ID] is provider
    hass.states.async_set("input_boolean.bath_heater", "on")
    await hass.async_block_till_done()
    assert provider.status is ACTIVE
    assert [t.new_status for t in fake_core.transitions] == [ACTIVE]


async def test_missing_source_repair_issue(hass: HomeAssistant, setup_plugin) -> None:
    """A source that exists neither in the registry nor as a state raises a repair issue."""
    from homeassistant.helpers import issue_registry as ir

    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER, "binary_sensor.gone"])
    issue_id = f"missing_sources_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["sources"] == "binary_sensor.gone"
    assert entry.runtime_data.missing_sources == ("binary_sensor.gone",)

    hass.states.async_set("binary_sensor.gone", "off")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    # Removed from the registry and the state machine: the issue comes back.
    registry = er.async_get(hass)
    registry.async_get_or_create("input_boolean", "input_boolean", "heater", suggested_object_id="heater")
    await hass.async_block_till_done()
    registry.async_remove(HEATER)
    hass.states.async_remove(HEATER)
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["sources"] == HEATER

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# ---------------------------------------------------------------------------
# Diagnostics and fallback device (review item 10)
# ---------------------------------------------------------------------------
async def test_diagnostics(hass: HomeAssistant, vtherm, setup_plugin) -> None:
    """Config entry diagnostics expose the config and the detector state."""
    from custom_components.vtherm_inhibition.diagnostics import async_get_config_entry_diagnostics

    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_SOURCE_ENTITIES] == [HEATER]
    assert diag["detector"]["status"] == "active"
    assert diag["detector"]["sources"][HEATER]["contribution"] == "active"
    assert diag["attached_providers"] == 1


async def test_fallback_device_removed_when_vtherm_device_exists(hass: HomeAssistant, setup_plugin) -> None:
    """The plugin's own device is removed once the VTherm has a device."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER])
    devices = dr.async_get(hass)
    assert devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id) is not None

    vt_entry = MockConfigEntry(domain=VT_DOMAIN, unique_id=VT_UNIQUE_ID)
    vt_entry.add_to_hass(hass)
    device = devices.async_get_or_create(config_entry_id=vt_entry.entry_id, identifiers={(VT_DOMAIN, VT_UNIQUE_ID)})
    er.async_get(hass).async_get_or_create(
        "climate", VT_DOMAIN, VT_UNIQUE_ID, suggested_object_id="bathroom", config_entry=vt_entry, device_id=device.id
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id) is None
    sensor = er.async_get(hass).async_get("binary_sensor.bathroom_inhibition")
    assert sensor is not None
    assert sensor.device_id == device.id


async def test_unload_awaits_unknown_before_unregistering(
    hass: HomeAssistant, vtherm, fake_core, setup_plugin, monkeypatch
) -> None:
    """The core has received (and finished handling) UNKNOWN when the factory is unregistered."""
    import asyncio

    from .conftest import FakeCoreAPI

    seen_at_unregister: list[list[InhibitionStatus]] = []
    handled: list[str] = []

    async def slow_core_callback(transition) -> None:
        await asyncio.sleep(0)
        handled.append(str(transition.new_status))

    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    fake_core.providers[VT_UNIQUE_ID].register_transition_callback(slow_core_callback)

    original = FakeCoreAPI.unregister_inhibition_provider_factory

    def _unregister(api, name: str) -> None:
        seen_at_unregister.append(list(handled))
        original(api, name)

    monkeypatch.setattr(FakeCoreAPI, "unregister_inhibition_provider_factory", _unregister)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert seen_at_unregister == [["unknown"]]
