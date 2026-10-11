"""Config flow and options flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_CLIMATE_HVAC_MODES,
    CONF_DEACTIVATION_DELAY,
    CONF_PHYSICAL_STOP_POLICY,
    CONF_SOURCE_ENTITIES,
    CONF_TARGET_ENTITY,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
    DOMAIN,
)

from .conftest import VT_ENTITY_ID, VT_UNIQUE_ID

SETTINGS = {
    CONF_SOURCE_ENTITIES: ["climate.bathroom_electric_heater_heater", "binary_sensor.window_block"],
    CONF_CLIMATE_HVAC_MODES: ["heat"],
    CONF_ACTIVATION_DELAY: 10.0,
    CONF_DEACTIVATION_DELAY: 120.0,
    CONF_UNAVAILABLE_GRACE_PERIOD: 600.0,
    CONF_PHYSICAL_STOP_POLICY: "do_not_turn_off",
}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_full_flow(hass: HomeAssistant, vtherm, fake_core) -> None:
    """Select a VTherm, then sources and settings: entry created."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_ENTITY: VT_ENTITY_ID})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "bathroom"  # state name of climate.bathroom
    assert result["data"] == {
        CONF_VTHERM_ENTITY_ID: VT_ENTITY_ID,
        CONF_VTHERM_UNIQUE_ID: VT_UNIQUE_ID,
        CONF_SOURCE_ENTITIES: ["climate.bathroom_electric_heater_heater", "binary_sensor.window_block"],
        CONF_CLIMATE_HVAC_MODES: ["heat"],
        CONF_ACTIVATION_DELAY: 10,
        CONF_DEACTIVATION_DELAY: 120,
        CONF_UNAVAILABLE_GRACE_PERIOD: 600,
        CONF_PHYSICAL_STOP_POLICY: "do_not_turn_off",
    }
    entry = result["result"]
    assert entry.unique_id == f"{DOMAIN}-{VT_UNIQUE_ID}"
    # The VTherm has been reloaded and the provider attached.
    assert VT_UNIQUE_ID in fake_core.providers


async def test_defaults_in_settings_form(hass: HomeAssistant, vtherm) -> None:
    """The settings form proposes the documented defaults."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_ENTITY: VT_ENTITY_ID})
    schema = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema if callable(getattr(key, "default", None))}
    assert defaults[CONF_CLIMATE_HVAC_MODES] == ["heat", "cool", "heat_cool", "auto", "dry"]
    assert defaults[CONF_ACTIVATION_DELAY] == 30
    assert defaults[CONF_DEACTIVATION_DELAY] == 60
    assert defaults[CONF_UNAVAILABLE_GRACE_PERIOD] == 300
    assert defaults[CONF_PHYSICAL_STOP_POLICY] == "turn_off"


async def test_not_a_vtherm(hass: HomeAssistant, vtherm) -> None:
    """A climate entity of another integration is refused."""
    er.async_get(hass).async_get_or_create("climate", "tado", "trv-1", suggested_object_id="trv")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_ENTITY: "climate.trv"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TARGET_ENTITY: "not_a_vtherm"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TARGET_ENTITY: "climate.not_registered"}
    )
    assert result["errors"] == {CONF_TARGET_ENTITY: "not_a_vtherm"}


async def test_source_validation(hass: HomeAssistant, vtherm) -> None:
    """At least one source; the VTherm cannot be its own source."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_ENTITY: VT_ENTITY_ID})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETTINGS, CONF_SOURCE_ENTITIES: []}
    )
    assert result["errors"] == {CONF_SOURCE_ENTITIES: "no_source"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETTINGS, CONF_SOURCE_ENTITIES: [VT_ENTITY_ID]}
    )
    assert result["errors"] == {CONF_SOURCE_ENTITIES: "source_is_target"}


async def test_duplicate_vtherm_aborts(hass: HomeAssistant, vtherm) -> None:
    """One entry per VTherm."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_ENTITY: VT_ENTITY_ID})
    await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    await hass.async_block_till_done()

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TARGET_ENTITY: VT_ENTITY_ID})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, vtherm, fake_core, setup_plugin, advance) -> None:
    """Options flow pre-fills current values, validates and applies them in place."""
    hass.states.async_set("input_boolean.heater", "on")
    hass.states.async_set("climate.ac", "cool")
    entry = await setup_plugin(["input_boolean.heater"], activation=5)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    defaults = {str(key): key.default() for key in schema if callable(getattr(key, "default", None))}
    assert defaults[CONF_SOURCE_ENTITIES] == ["input_boolean.heater"]
    assert defaults[CONF_ACTIVATION_DELAY] == 5

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**SETTINGS, CONF_SOURCE_ENTITIES: [VT_ENTITY_ID]}
    )
    assert result["errors"] == {CONF_SOURCE_ENTITIES: "source_is_target"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**SETTINGS, CONF_SOURCE_ENTITIES: ["climate.ac"], CONF_CLIMATE_HVAC_MODES: ["heat"]},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SOURCE_ENTITIES] == ["climate.ac"]
    assert entry.options[CONF_ACTIVATION_DELAY] == 10
    detector = entry.runtime_data
    assert detector.config.source_entities == ("climate.ac",)
    assert detector.config.physical_stop_policy == "do_not_turn_off"
    # climate.ac is in "cool", which is not an inhibiting mode any more: the raw
    # change goes through the new deactivation delay, on the same provider.
    provider = fake_core.providers[VT_UNIQUE_ID]
    assert fake_core.attach_count == 1
    assert provider.status == "active"
    await advance(120)
    assert provider.status == "inactive"
    assert [t.new_status for t in fake_core.transitions] == ["inactive"]
