"""Shared fixtures: a fake VTherm core reached through the vtherm_api facade."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_fire_time_changed,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
from vtherm_api import VThermAPI

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
from custom_components.vtherm_inhibition.models import InhibitionStatus

VT_UNIQUE_ID = "vt-bathroom-uid"
VT_ENTITY_ID = "climate.bathroom"
VT_UNDERLYING = "climate.bathroom_trv"
VT_UNDERLYING_2 = "climate.bathroom_trv_2"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable the custom integration in every test."""
    yield


class FakeCoreAPI(VThermAPI):
    """The core's API instance: records the facade calls instead of reaching a VTherm.

    Like the real facade, a call for an entity without state raises
    ``LookupError``. ``errors`` holds exceptions raised by the next calls (one
    per call); ``gate`` (an ``asyncio.Event``), when set, makes the calls wait
    for it (a slow exit reconciliation).
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, bool | None]] = []
        self.errors: list[BaseException] = []
        self.gate: asyncio.Event | None = None

    async def _call(self, name: str, entity_id: str, stop_underlyings: bool | None) -> None:
        if self.hass.states.get(entity_id) is None:
            raise LookupError(f"No VTherm entity found for inhibition: {entity_id}")
        self.calls.append((name, entity_id, stop_underlyings))
        if self.gate is not None:
            await self.gate.wait()
        if self.errors:
            raise self.errors.pop(0)

    async def set_inhibition(self, vtherm_entity_id: str, stop_underlyings: bool) -> None:
        await self._call("set", vtherm_entity_id, stop_underlyings)

    async def unset_inhibition(self, vtherm_entity_id: str) -> None:
        await self._call("unset", vtherm_entity_id, None)

    @property
    def names(self) -> list[str]:
        """The calls, without their arguments."""
        return [name for name, _, _ in self.calls]


@pytest.fixture(autouse=True)
def core_api(hass: HomeAssistant) -> FakeCoreAPI:
    """Give every test a fresh core API singleton (created as the core does)."""
    VThermAPI.reset_vtherm_api()
    api = FakeCoreAPI.get_vtherm_api(hass)
    yield api
    VThermAPI.reset_vtherm_api()


class _FakeVThermFlow(ConfigFlow):
    """Config flow handler required to load a fake versatile_thermostat entry."""

    VERSION = 1


def set_vtherm_state(hass: HomeAssistant, ready: bool = True, entity_id: str = VT_ENTITY_ID) -> None:
    """Write the VTherm state (``is_ready`` as published by the core)."""
    hass.states.async_set(entity_id, "heat", {"is_ready": ready})


@pytest.fixture
async def vtherm(hass: HomeAssistant):
    """A loaded fake versatile_thermostat entry with one registered, ready climate entity."""

    async def _async_setup_entry(hass_: HomeAssistant, entry: ConfigEntry) -> bool:
        return True

    mock_integration(
        hass,
        MockModule(VT_DOMAIN, async_setup_entry=_async_setup_entry, async_unload_entry=_async_setup_entry),
    )
    mock_platform(hass, f"{VT_DOMAIN}.config_flow", None)
    vt_entry = MockConfigEntry(
        domain=VT_DOMAIN,
        unique_id=VT_UNIQUE_ID,
        title="Bathroom",
        data={"underlying_entity_ids": [VT_UNDERLYING], "opening_degree_entity_ids": ["number.bathroom_trv_opening"]},
        options={"climate_entity2_id": VT_UNDERLYING_2, "sync_entity_ids": None},
    )
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
    set_vtherm_state(hass)
    with mock_config_flow(VT_DOMAIN, _FakeVThermFlow):
        assert await hass.config_entries.async_setup(vt_entry.entry_id)
        await hass.async_block_till_done()
        yield vt_entry


def plugin_entry(
    sources: list[str],
    *,
    activation: int = 0,
    deactivation: int = 0,
    grace: int = 0,
    unique_id: str = VT_UNIQUE_ID,
    entity_id: str = VT_ENTITY_ID,
    version: int = 2,
    options: dict[str, Any] | None = None,
    **extra: Any,
) -> MockConfigEntry:
    """Build a plugin config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Bathroom",
        unique_id=f"{DOMAIN}-{unique_id}",
        version=version,
        data={
            CONF_VTHERM_ENTITY_ID: entity_id,
            CONF_VTHERM_UNIQUE_ID: unique_id,
            CONF_SOURCE_ENTITIES: sources,
            CONF_ACTIVATION_DELAY: activation,
            CONF_DEACTIVATION_DELAY: deactivation,
            CONF_UNAVAILABLE_GRACE_PERIOD: grace,
            **extra,
        },
        options=options or {},
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

    async def _advance(seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    return _advance


class StatusRecorder:
    """The confirmed status of a detector and its changes since attached."""

    def __init__(self, detector) -> None:
        self._detector = detector
        self._last = detector.status
        self.transitions: list[InhibitionStatus] = []
        detector.add_update_listener(self._updated)

    def _updated(self) -> None:
        if self._detector.status != self._last:
            self._last = self._detector.status
            self.transitions.append(self._last)

    @property
    def status(self) -> InhibitionStatus:
        return self._detector.status

    @property
    def statuses(self) -> list[InhibitionStatus]:
        return list(self.transitions)


@pytest.fixture
def attach(hass: HomeAssistant):
    """Return a coroutine returning (view, recorder) of the last plugin entry's confirmed status."""

    async def _attach(entry: ConfigEntry | None = None):
        entry = entry or hass.config_entries.async_entries(DOMAIN)[-1]
        recorder = StatusRecorder(entry.runtime_data.detector)
        return recorder, recorder

    return _attach
