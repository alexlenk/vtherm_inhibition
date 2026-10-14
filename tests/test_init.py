"""Entry lifecycle, migration, entities, repair issues and diagnostics."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vtherm_inhibition.const import (
    CONF_LEGACY_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_STOP_UNDERLYINGS,
    DOMAIN,
    VT_DOMAIN,
)
from custom_components.vtherm_inhibition.models import InhibitionStatus

from .conftest import VT_ENTITY_ID, VT_UNIQUE_ID, plugin_entry

ACTIVE = InhibitionStatus.ACTIVE
INACTIVE = InhibitionStatus.INACTIVE
UNKNOWN = InhibitionStatus.UNKNOWN
HEATER = "input_boolean.heater"
SENSOR = "binary_sensor.bathroom_inhibition"


# ---------------------------------------------------------------------------
# Migration of version 1 entries
# ---------------------------------------------------------------------------
async def test_migrate_v1_entry(hass: HomeAssistant, vtherm, core_api) -> None:
    """physical_stop_policy (data and options) becomes stop_underlyings; the options win."""
    hass.states.async_set(HEATER, "on")
    entry = plugin_entry(
        [HEATER],
        version=1,
        physical_stop_policy="turn_off",
        options={CONF_SOURCE_ENTITIES: [HEATER], CONF_LEGACY_PHYSICAL_STOP_POLICY: "do_not_turn_off"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert CONF_LEGACY_PHYSICAL_STOP_POLICY not in entry.data
    assert CONF_LEGACY_PHYSICAL_STOP_POLICY not in entry.options
    assert entry.data[CONF_STOP_UNDERLYINGS] is True
    assert entry.options[CONF_STOP_UNDERLYINGS] is False
    assert entry.runtime_data.detector.config.stop_underlyings is False
    assert core_api.calls == [("set", VT_ENTITY_ID, False)]


async def test_migrate_v1_entry_without_policy(hass: HomeAssistant) -> None:
    """A version 1 entry that never stored a policy gets the default (True)."""
    hass.states.async_set(HEATER, "off")
    entry = plugin_entry([HEATER], version=1)
    entry.add_to_hass(hass)
    from pytest_homeassistant_custom_component.common import MockModule, mock_integration

    mock_integration(hass, MockModule(VT_DOMAIN))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2
    assert CONF_STOP_UNDERLYINGS not in entry.data
    assert entry.runtime_data.detector.config.stop_underlyings is True


async def test_future_version_not_loaded(hass: HomeAssistant) -> None:
    """An entry from a newer version of the plugin is not touched."""
    from pytest_homeassistant_custom_component.common import MockModule, mock_integration

    mock_integration(hass, MockModule(VT_DOMAIN))
    entry = plugin_entry([HEATER], version=3)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
async def test_entities_on_vtherm_device(hass: HomeAssistant, vtherm, setup_plugin) -> None:
    """Entities are attached to the VTherm device, unique IDs derive from the VTherm."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    registry = er.async_get(hass)
    sensor = registry.async_get(SENSOR)
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
    assert er.async_get(hass).async_get(SENSOR).device_id == device.id


async def test_binary_sensor_states_and_attributes(hass: HomeAssistant, vtherm, core_api, setup_plugin, advance) -> None:
    """on/off/unknown plus the observability attributes (status, sources, API calls)."""
    ac = "climate.bathroom_ac"
    hass.states.async_set(HEATER, "off")
    hass.states.async_set(ac, "off")
    entry = await setup_plugin([HEATER, ac], activation=30, grace=60, stop_underlyings=False)
    state = hass.states.get(SENSOR)
    assert state.state == "off"
    attrs = state.attributes
    assert attrs["status"] == "inactive"
    assert attrs["raw_status"] == "inactive"
    assert attrs["stop_underlyings"] is False
    assert attrs["bypass"] is False
    assert attrs["active_sources"] == []
    assert attrs["pending_status"] is None
    assert attrs["sources"] == {
        HEATER: {"state": "off", "contribution": "inactive"},
        ac: {"state": "off", "contribution": "inactive"},
    }
    assert attrs["vtherm_entity_id"] == VT_ENTITY_ID
    assert attrs["vtherm_available"] is True
    assert attrs["last_api_call"] == "unset"
    assert attrs["last_api_call_at"] is not None
    assert attrs["last_error"] is None
    assert attrs["retry_pending"] is False
    assert attrs["next_retry_at"] is None

    hass.states.async_set(ac, "cool")
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "off"
    assert state.attributes["raw_status"] == "active"
    assert state.attributes["pending_status"] == "active"
    assert state.attributes["pending_until"] is not None
    assert state.attributes["sources"][ac] == {"state": "cool", "contribution": "active"}
    assert state.attributes["active_sources"] == [ac]

    await advance(30)
    state = hass.states.get(SENSOR)
    assert state.state == "on"
    assert state.attributes["status"] == "active"
    assert state.attributes["last_change"] is not None
    assert state.attributes["last_api_call"] == "set"

    hass.states.async_set(ac, "unavailable")
    await advance(60)
    state = hass.states.get(SENSOR)
    assert state.state == "unknown"
    assert state.attributes["status"] == "unknown"
    assert state.attributes["last_api_call"] == "set"
    assert entry.runtime_data.detector.status is UNKNOWN
    assert core_api.calls == [("unset", VT_ENTITY_ID, None), ("set", VT_ENTITY_ID, False)]


# ---------------------------------------------------------------------------
# Renamed / missing sources
# ---------------------------------------------------------------------------
async def test_renamed_source_is_followed(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """A renamed source is replaced in the options and tracked under its new ID."""
    registry = er.async_get(hass)
    registry.async_get_or_create("input_boolean", "input_boolean", "heater", suggested_object_id="heater")
    hass.states.async_set(HEATER, "off")
    hass.states.async_set("binary_sensor.other", "off")
    # With a grace period, the rename blip is invisible.
    entry = await setup_plugin([HEATER, "binary_sensor.other"], grace=60)

    registry.async_update_entity(HEATER, new_entity_id="input_boolean.bath_heater")
    hass.states.async_remove(HEATER)
    hass.states.async_set("input_boolean.bath_heater", "off")
    await hass.async_block_till_done()

    assert entry.options[CONF_SOURCE_ENTITIES] == ["input_boolean.bath_heater", "binary_sensor.other"]
    detector = entry.runtime_data.detector
    assert detector.config.source_entities == ("input_boolean.bath_heater", "binary_sensor.other")
    assert core_api.names == ["unset"]
    hass.states.async_set("input_boolean.bath_heater", "on")
    await hass.async_block_till_done()
    assert detector.status is ACTIVE
    assert core_api.names == ["unset", "set"]


async def test_missing_source_repair_issue(hass: HomeAssistant, setup_plugin) -> None:
    """A source that exists neither in the registry nor as a state raises a repair issue."""
    hass.states.async_set(HEATER, "off")
    entry = await setup_plugin([HEATER, "binary_sensor.gone"])
    issue_id = f"missing_sources_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["sources"] == "binary_sensor.gone"
    assert entry.runtime_data.detector.missing_sources == ("binary_sensor.gone",)

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
# Options
# ---------------------------------------------------------------------------
async def test_options_change_in_place(hass: HomeAssistant, vtherm, core_api, setup_plugin) -> None:
    """Saving the options neither reloads the entry nor calls the API without a status change."""
    hass.states.async_set(HEATER, "on")
    hass.states.async_set("binary_sensor.other", "off")
    entry = await setup_plugin([HEATER])
    runtime = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, options={CONF_SOURCE_ENTITIES: [HEATER, "binary_sensor.other"], CONF_STOP_UNDERLYINGS: False}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is runtime
    assert runtime.detector.config.stop_underlyings is False
    assert core_api.calls == [("set", VT_ENTITY_ID, True)]
    assert hass.states.get(SENSOR).attributes["stop_underlyings"] is False


# ---------------------------------------------------------------------------
# Diagnostics and fallback device
# ---------------------------------------------------------------------------
async def test_diagnostics(hass: HomeAssistant, vtherm, setup_plugin) -> None:
    """Config entry diagnostics expose the config, the detector state and the API calls."""
    from custom_components.vtherm_inhibition.diagnostics import async_get_config_entry_diagnostics

    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_SOURCE_ENTITIES] == [HEATER]
    assert diag["detector"]["status"] == "active"
    assert diag["detector"]["sources"][HEATER]["contribution"] == "active"
    assert diag["link"]["last_api_call"] == "set"
    assert diag["link"]["vtherm_entity_id"] == VT_ENTITY_ID


async def test_fallback_device_removed_when_vtherm_device_exists(hass: HomeAssistant, setup_plugin) -> None:
    """The plugin's own device is removed once the VTherm has a device."""
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
    sensor = er.async_get(hass).async_get(SENSOR)
    assert sensor is not None
    assert sensor.device_id == device.id
