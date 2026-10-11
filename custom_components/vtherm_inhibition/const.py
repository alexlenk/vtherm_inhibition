"""Constants for the VTherm inhibition integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "vtherm_inhibition"
VT_DOMAIN = "versatile_thermostat"

#: Stable provider / factory name registered in the VTherm API.
PROVIDER_NAME = "vtherm_inhibition"

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SWITCH]

# Keys of hass.data[DOMAIN]
DATA_DETECTORS = "detectors"
DATA_FACTORY = "factory"

# Configuration keys (config entry data / options)
CONF_VTHERM_ENTITY_ID = "vtherm_entity_id"
CONF_VTHERM_UNIQUE_ID = "vtherm_unique_id"
CONF_TARGET_ENTITY = "target_entity"
CONF_SOURCE_ENTITIES = "source_entities"
CONF_CLIMATE_HVAC_MODES = "climate_hvac_modes"
CONF_ACTIVATION_DELAY = "activation_delay"
CONF_DEACTIVATION_DELAY = "deactivation_delay"
CONF_UNAVAILABLE_GRACE_PERIOD = "unavailable_grace_period"
CONF_PHYSICAL_STOP_POLICY = "physical_stop_policy"

# Source domains accepted by the plugin.
SOURCE_DOMAINS_ON_OFF = ("binary_sensor", "input_boolean", "switch")
SOURCE_DOMAIN_CLIMATE = "climate"
SOURCE_DOMAINS = (*SOURCE_DOMAINS_ON_OFF, SOURCE_DOMAIN_CLIMATE)

# HVAC modes a climate source may be configured to inhibit on.
SELECTABLE_HVAC_MODES = ["heat", "cool", "heat_cool", "auto", "dry", "fan_only"]

DEFAULT_CLIMATE_HVAC_MODES = ["heat", "cool", "heat_cool", "auto", "dry"]
DEFAULT_ACTIVATION_DELAY = 30
DEFAULT_DEACTIVATION_DELAY = 60
DEFAULT_UNAVAILABLE_GRACE_PERIOD = 300
DEFAULT_PHYSICAL_STOP_POLICY = "turn_off"
MAX_DELAY_SECONDS = 86400

# Per-source contribution, exposed in the diagnostic attributes.
SOURCE_ACTIVE = "active"
SOURCE_INACTIVE = "inactive"
SOURCE_UNAVAILABLE = "unavailable"
