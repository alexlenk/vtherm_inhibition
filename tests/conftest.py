"""Shared fixtures: a fake VTherm core that only uses the vtherm_api contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.config_entries import ConfigFlow
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from vtherm_api import InhibitionStatus, InhibitionTransition, VThermAPI

from custom_components.vtherm_inhibition.const import (
    CONF_ACTIVATION_DELAY,
    CONF_DEACTIVATION_DELAY,
    CONF_SOURCE_ENTITIES,
    CONF_UNAVAILABLE_GRACE_PERIOD,
    CONF_VTHERM_ENTITY_ID,
    CONF_VTHERM_UNIQUE_ID,
    DOMAIN,
    VT_DOMAIN,
)

VT_UNIQUE_ID = "vt-bathroom-uid"
VT_ENTITY_ID = "climate.bathroom"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
def reset_vtherm_api(hass: HomeAssistant):
    """Give every test a fresh VThermAPI singleton."""
    VThermAPI.reset_vtherm_api()
    VThermAPI.get_vtherm_api(hass)
    yield
    VThermAPI.reset_vtherm_api()


class _FakeVThermFlow(ConfigFlow):
    """Config flow handler required to load a fake versatile_thermostat entry."""

    VERSION = 1


@dataclass
class FakeThermostat:
    """Runtime view of a VTherm (only what the factory/provider may read)."""

    hass: HomeAssistant
    unique_id: str
    entity_id: str
    name: str = "Bathroom"
    entry_infos: dict[str, Any] = field(default_factory=dict)


class FakeCore:
    """Mimics the core discovery: supports -> create -> post_init -> start ->
    restore_state -> register callback -> async_refresh_state."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.providers: dict[str, Any] = {}
        self.statuses: dict[str, InhibitionStatus] = {}
        self.transitions: list[InhibitionTransition] = []
        self.removers: dict[str, Callable[[], None]] = {}
        self.attach_count = 0

    async def _on_transition(self, transition: InhibitionTransition) -> None:
        self.transitions.append(transition)

    async def attach(self, thermostat: FakeThermostat) -> InhibitionStatus | None:
        api = VThermAPI.get_vtherm_api(self.hass)
        for factory in api.get_inhibition_provider_factories():
            if not factory.supports(thermostat):
                continue
            provider = factory.create(thermostat)
            provider.post_init(thermostat.entry_infos)
            self.removers[thermostat.unique_id] = provider.register_transition_callback(self._on_transition)
            await provider.async_start_listening()
            provider.restore_state(None)
            status = await provider.async_refresh_state()
            self.providers[thermostat.unique_id] = provider
            self.statuses[thermostat.unique_id] = status
            self.attach_count += 1
            return status
        self.providers.pop(thermostat.unique_id, None)
        return None

    def detach(self, thermostat: FakeThermostat) -> None:
        provider = self.providers.pop(thermostat.unique_id, None)
        if provider is not None:
            provider.stop_listening()


@pytest.fixture
def fake_core(hass: HomeAssistant) -> FakeCore:
    """A fake core instance."""
    return FakeCore(hass)


@pytest.fixture
def thermostat(hass: HomeAssistant) -> FakeThermostat:
    """The runtime view of the bathroom VTherm."""
    return FakeThermostat(hass, VT_UNIQUE_ID, VT_ENTITY_ID)


@pytest.fixture
async def vtherm(hass: HomeAssistant, fake_core: FakeCore, thermostat: FakeThermostat):
    """A loaded fake versatile_thermostat entry with one climate entity.

    Its setup runs the fake core discovery, like the real core's
    ``async_startup`` does after ``init_vtherm_links``.
    """

    async def async_setup_entry(hass_: HomeAssistant, entry: ConfigEntry) -> bool:
        await fake_core.attach(thermostat)
        return True

    async def async_unload_entry(hass_: HomeAssistant, entry: ConfigEntry) -> bool:
        fake_core.detach(thermostat)
        return True

    mock_integration(
        hass,
        MockModule(VT_DOMAIN, async_setup_entry=async_setup_entry, async_unload_entry=async_unload_entry),
    )
    mock_platform(hass, f"{VT_DOMAIN}.config_flow", None)
    vt_entry = MockConfigEntry(domain=VT_DOMAIN, unique_id=VT_UNIQUE_ID, title="Bathroom")
    vt_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=vt_entry.entry_id, identifiers={(VT_DOMAIN, VT_UNIQUE_ID)}, name="Bathroom"
    )
    er.async_get(hass).async_get_or_create(
        "climate",
        VT_DOMAIN,
        VT_UNIQUE_ID,
        suggested_object_id="bathroom",
        config_entry=vt_entry,
        device_id=device.id,
    )
    hass.states.async_set(VT_ENTITY_ID, "heat")
    with mock_config_flow(VT_DOMAIN, _FakeVThermFlow):
        assert await hass.config_entries.async_setup(vt_entry.entry_id)
        await hass.async_block_till_done()
        yield vt_entry


@pytest.fixture
def vt_module(hass: HomeAssistant) -> None:
    """Register a bare versatile_thermostat integration (manifest dependency)."""
    mock_integration(hass, MockModule(VT_DOMAIN))


def plugin_entry(
    sources: list[str],
    *,
    activation: int = 0,
    deactivation: int = 0,
    grace: int = 0,
    unique_id: str = VT_UNIQUE_ID,
    entity_id: str = VT_ENTITY_ID,
    **extra: Any,
) -> MockConfigEntry:
    """Build a plugin config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Bathroom",
        unique_id=f"{DOMAIN}-{unique_id}",
        data={
            CONF_VTHERM_ENTITY_ID: entity_id,
            CONF_VTHERM_UNIQUE_ID: unique_id,
            CONF_SOURCE_ENTITIES: sources,
            CONF_ACTIVATION_DELAY: activation,
            CONF_DEACTIVATION_DELAY: deactivation,
            CONF_UNAVAILABLE_GRACE_PERIOD: grace,
            **extra,
        },
    )


@pytest.fixture
def setup_plugin(hass: HomeAssistant) -> Callable[..., Awaitable[MockConfigEntry]]:
    """Return a coroutine function adding and setting up a plugin entry."""

    async def _setup(sources: list[str], **kwargs: Any) -> MockConfigEntry:
        if not hass.config_entries.async_entries(VT_DOMAIN):
            mock_integration(hass, MockModule(VT_DOMAIN))
        entry = plugin_entry(sources, **kwargs)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


@pytest.fixture
def advance(hass: HomeAssistant, freezer):
    """Return a coroutine function moving the clock forward by N seconds."""
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async def _advance(seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    return _advance


class Recorder:
    """Records the transitions delivered to a callback."""

    def __init__(self) -> None:
        self.transitions: list[InhibitionTransition] = []

    async def __call__(self, transition: InhibitionTransition) -> None:
        self.transitions.append(transition)

    @property
    def statuses(self) -> list[InhibitionStatus]:
        return [t.new_status for t in self.transitions]


@pytest.fixture
def attach(hass: HomeAssistant, thermostat: FakeThermostat):
    """Return a coroutine creating, starting and refreshing a provider."""
    from custom_components.vtherm_inhibition.factory import InhibitionProviderFactory

    async def _attach(target: FakeThermostat | None = None):
        provider = InhibitionProviderFactory(hass).create(target or thermostat)
        recorder = Recorder()
        provider.register_transition_callback(recorder)
        await provider.async_start_listening()
        await provider.async_refresh_state()
        await hass.async_block_till_done()
        return provider, recorder

    return _attach
