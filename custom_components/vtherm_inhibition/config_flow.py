"""Config flow for the VTherm inhibition plugin (one entry per VTherm)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
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
    MAX_DELAY_SECONDS,
    SELECTABLE_HVAC_MODES,
    SOURCE_DOMAINS,
    VT_DOMAIN,
)
from .models import OPTION_KEYS, default_options, normalize_policy


def _seconds_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=MAX_DELAY_SECONDS,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema shared by the creation step and the options flow."""
    return vol.Schema(
        {
            vol.Required(CONF_SOURCE_ENTITIES, default=defaults[CONF_SOURCE_ENTITIES]): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=list(SOURCE_DOMAINS), multiple=True)
            ),
            vol.Optional(CONF_CLIMATE_HVAC_MODES, default=defaults[CONF_CLIMATE_HVAC_MODES]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=SELECTABLE_HVAC_MODES,
                    multiple=True,
                    translation_key="hvac_mode",
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(CONF_ACTIVATION_DELAY, default=defaults[CONF_ACTIVATION_DELAY]): _seconds_selector(),
            vol.Optional(CONF_DEACTIVATION_DELAY, default=defaults[CONF_DEACTIVATION_DELAY]): _seconds_selector(),
            vol.Optional(
                CONF_UNAVAILABLE_GRACE_PERIOD, default=defaults[CONF_UNAVAILABLE_GRACE_PERIOD]
            ): _seconds_selector(),
            vol.Optional(CONF_PHYSICAL_STOP_POLICY, default=defaults[CONF_PHYSICAL_STOP_POLICY]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["turn_off", "do_not_turn_off"],
                    translation_key="physical_stop_policy",
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _clean_settings(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize the submitted settings (integers, valid policy)."""
    settings = {key: user_input[key] for key in OPTION_KEYS if key in user_input}
    for key in (CONF_ACTIVATION_DELAY, CONF_DEACTIVATION_DELAY, CONF_UNAVAILABLE_GRACE_PERIOD):
        if key in settings:
            settings[key] = int(settings[key])
    if CONF_PHYSICAL_STOP_POLICY in settings:
        settings[CONF_PHYSICAL_STOP_POLICY] = str(normalize_policy(settings[CONF_PHYSICAL_STOP_POLICY]))
    settings[CONF_SOURCE_ENTITIES] = list(dict.fromkeys(settings.get(CONF_SOURCE_ENTITIES) or []))
    return settings


def _validate_sources(user_input: dict[str, Any], vtherm_entity_id: str) -> dict[str, str]:
    sources = user_input.get(CONF_SOURCE_ENTITIES) or []
    if not sources:
        return {CONF_SOURCE_ENTITIES: "no_source"}
    if vtherm_entity_id in sources:
        return {CONF_SOURCE_ENTITIES: "source_is_target"}
    return {}


class InhibitionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one inhibition entry per VTherm."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the selected VTherm kept between steps."""
        super().__init__()
        self._vtherm_entity_id = ""
        self._vtherm_unique_id = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select the Versatile Thermostat."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = user_input[CONF_TARGET_ENTITY]
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry is None or registry_entry.platform != VT_DOMAIN or not registry_entry.unique_id:
                errors[CONF_TARGET_ENTITY] = "not_a_vtherm"
            else:
                self._vtherm_entity_id = entity_id
                self._vtherm_unique_id = registry_entry.unique_id
                await self.async_set_unique_id(f"{DOMAIN}-{self._vtherm_unique_id}")
                self._abort_if_unique_id_configured()
                return await self.async_step_settings()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=CLIMATE_DOMAIN, integration=VT_DOMAIN)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select the sources, delays and policy."""
        errors: dict[str, str] = {}
        defaults = default_options()
        if user_input is not None:
            errors = _validate_sources(user_input, self._vtherm_entity_id)
            if not errors:
                data = {
                    CONF_VTHERM_ENTITY_ID: self._vtherm_entity_id,
                    CONF_VTHERM_UNIQUE_ID: self._vtherm_unique_id,
                    **_clean_settings(user_input),
                }
                state = self.hass.states.get(self._vtherm_entity_id)
                return self.async_create_entry(title=state.name if state else self._vtherm_entity_id, data=data)
            defaults.update(user_input)

        return self.async_show_form(
            step_id="settings",
            data_schema=settings_schema(defaults),
            errors=errors,
            description_placeholders={"vtherm": self._vtherm_entity_id},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return InhibitionOptionsFlow()


class InhibitionOptionsFlow(OptionsFlow):
    """Edit the sources, delays and policy of an entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and save the settings."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        defaults = default_options()
        defaults.update({key: value for key, value in entry.data.items() if key in OPTION_KEYS})
        defaults.update({key: value for key, value in entry.options.items() if key in OPTION_KEYS})
        if user_input is not None:
            errors = _validate_sources(user_input, entry.data.get(CONF_VTHERM_ENTITY_ID, ""))
            if not errors:
                return self.async_create_entry(title="", data=_clean_settings(user_input))
            defaults.update(user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=settings_schema(defaults),
            errors=errors,
            description_placeholders={"vtherm": entry.data.get(CONF_VTHERM_ENTITY_ID, "")},
        )
