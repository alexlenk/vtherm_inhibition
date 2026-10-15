"""Constants for the VTherm inhibition integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "vtherm_inhibition"
VT_DOMAIN = "versatile_thermostat"

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SWITCH]

# Configuration keys (config entry data / options)
CONF_VTHERM_ENTITY_ID = "vtherm_entity_id"
CONF_VTHERM_UNIQUE_ID = "vtherm_unique_id"
CONF_TARGET_ENTITY = "target_entity"
CONF_SOURCE_ENTITIES = "source_entities"
CONF_CLIMATE_HVAC_MODES = "climate_hvac_modes"
CONF_ACTIVATION_DELAY = "activation_delay"
CONF_DEACTIVATION_DELAY = "deactivation_delay"
CONF_UNAVAILABLE_GRACE_PERIOD = "unavailable_grace_period"
CONF_STOP_UNDERLYINGS = "stop_underlyings"
#: Option of config entry version 1, migrated to ``stop_underlyings``.
CONF_LEGACY_PHYSICAL_STOP_POLICY = "physical_stop_policy"
LEGACY_POLICY_DO_NOT_TURN_OFF = "do_not_turn_off"

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
DEFAULT_STOP_UNDERLYINGS = True
MAX_DELAY_SECONDS = 86400

#: Delays (seconds) before the 1st, 2nd and 3rd retry of a failed API call;
#: later retries run every RETRY_INTERVAL seconds.
RETRY_DELAYS = (30, 60, 120)
RETRY_INTERVAL = 300
#: Seconds between two checks while waiting for the VTherm to restore its state at startup.
START_POLL_INTERVAL = 5
#: Seconds a deleted entry waits for a VTherm that has not started yet before releasing it anyway.
RELEASE_START_TIMEOUT = 300

# Per-source contribution, exposed in the diagnostic attributes.
SOURCE_ACTIVE = "active"
SOURCE_INACTIVE = "inactive"
SOURCE_UNAVAILABLE = "unavailable"
