"""Provider contract: transitions, sequence, callbacks, stop, policy."""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant
from vtherm_api import (
    InhibitionStatus,
    InhibitionStopPolicy,
    InterfaceInhibitionProvider,
    InterfaceInhibitionProviderFactory,
)

from custom_components.vtherm_inhibition.factory import InhibitionProviderFactory

from .conftest import Recorder

ACTIVE = InhibitionStatus.ACTIVE
INACTIVE = InhibitionStatus.INACTIVE
UNKNOWN = InhibitionStatus.UNKNOWN
HEATER = "input_boolean.heater"


async def _toggle(hass: HomeAssistant, *states: str) -> None:
    for state in states:
        hass.states.async_set(HEATER, state)
        await hass.async_block_till_done()


async def test_protocols(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Factory and provider satisfy the runtime-checkable vtherm_api protocols."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    provider, _ = await attach()
    assert isinstance(InhibitionProviderFactory(hass), InterfaceInhibitionProviderFactory)
    assert isinstance(provider, InterfaceInhibitionProvider)
    assert provider.name == "vtherm_inhibition"


async def test_transition_content_and_sequence(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Each confirmed change emits one transition with a strictly increasing sequence."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    provider, rec = await attach()

    await _toggle(hass, "on", "on", "off", "unavailable", "on")
    assert rec.statuses == [ACTIVE, INACTIVE, UNKNOWN, ACTIVE]
    assert [t.old_status for t in rec.transitions] == [INACTIVE, ACTIVE, INACTIVE, UNKNOWN]
    assert [t.sequence for t in rec.transitions] == [1, 2, 3, 4]
    assert all(t.provider_name == "vtherm_inhibition" for t in rec.transitions)
    assert all(t.stop_policy is InhibitionStopPolicy.TURN_OFF for t in rec.transitions)
    assert all(t.timestamp.tzinfo is not None for t in rec.transitions)
    assert provider.sequence == 4

    provider.stop_listening()
    await hass.async_block_till_done()
    assert rec.transitions[-1].sequence == 5
    assert rec.transitions[-1].new_status is UNKNOWN
    assert rec.transitions[-1].old_status is ACTIVE


async def test_callbacks_awaited_in_order(hass: HomeAssistant, setup_plugin, attach) -> None:
    """Callbacks are awaited one by one, transitions never overlap."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    provider, _ = await attach()
    events: list[str] = []

    async def slow(transition) -> None:
        events.append(f"slow-start-{transition.sequence}")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        events.append(f"slow-end-{transition.sequence}")

    async def fast(transition) -> None:
        events.append(f"fast-{transition.sequence}")

    provider.register_transition_callback(slow)
    provider.register_transition_callback(fast)
    hass.states.async_set(HEATER, "on")
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert events == [
        "slow-start-1",
        "slow-end-1",
        "fast-1",
        "slow-start-2",
        "slow-end-2",
        "fast-2",
    ]


async def test_failing_callback_does_not_break_provider(hass: HomeAssistant, setup_plugin, attach, caplog) -> None:
    """A raising callback is logged; the others and later transitions still run."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    provider, rec = await attach()

    async def broken(transition) -> None:
        raise RuntimeError("boom")

    # Registered after the recorder, and a second recorder after it.
    provider.register_transition_callback(broken)
    rec2 = Recorder()
    provider.register_transition_callback(rec2)

    await _toggle(hass, "on", "off")
    assert rec.statuses == [ACTIVE, INACTIVE]
    assert rec2.statuses == [ACTIVE, INACTIVE]
    assert "transition callback failed" in caplog.text


async def test_remove_callback(hass: HomeAssistant, setup_plugin, attach) -> None:
    """The remover returned by register_transition_callback detaches it."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    provider, _ = await attach()
    rec = Recorder()
    remove = provider.register_transition_callback(rec)
    await _toggle(hass, "on")
    remove()
    remove()  # idempotent
    await _toggle(hass, "off")
    assert rec.statuses == [ACTIVE]


async def test_stop_listening_reports_unknown_before_detach(hass: HomeAssistant, setup_plugin, attach) -> None:
    """stop_listening: UNKNOWN delivered (eagerly), then nothing more."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    provider, rec = await attach()
    assert provider in entry.runtime_data.providers

    provider.stop_listening()
    # Delivered synchronously when the callback does not block.
    assert rec.statuses == [UNKNOWN]
    assert provider.status is UNKNOWN
    assert provider.is_listening is False
    assert provider not in entry.runtime_data.providers
    assert await provider.async_refresh_state() is UNKNOWN

    await _toggle(hass, "off", "on")
    provider.stop_listening()  # idempotent: callbacks already detached
    await hass.async_block_till_done()
    assert rec.statuses == [UNKNOWN]


async def test_stop_listening_without_callbacks(hass: HomeAssistant, setup_plugin) -> None:
    """Stopping a provider without callbacks emits nothing and consumes no sequence."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    provider = InhibitionProviderFactory(hass).create(
        type("T", (), {"unique_id": "vt-bathroom-uid", "entity_id": "climate.bathroom", "name": "B"})()
    )
    await provider.async_start_listening()
    provider.stop_listening()
    await hass.async_block_till_done()
    assert provider.sequence == 0
    assert provider.status is UNKNOWN


async def test_restart_after_stop(hass: HomeAssistant, setup_plugin, attach) -> None:
    """After stop, a new start reports the actual status to newly registered callbacks."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    provider, rec = await attach()
    provider.stop_listening()
    rec2 = Recorder()
    provider.register_transition_callback(rec2)
    await provider.async_start_listening()
    await provider.async_start_listening()  # idempotent
    await hass.async_block_till_done()
    assert provider.status is ACTIVE
    assert rec2.statuses == [ACTIVE]
    assert rec2.transitions[0].old_status is UNKNOWN
    assert rec2.transitions[0].sequence == 2


async def test_no_transition_before_start(hass: HomeAssistant, setup_plugin, thermostat) -> None:
    """A created but not started provider receives no transition."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    provider = InhibitionProviderFactory(hass).create(thermostat)
    rec = Recorder()
    provider.register_transition_callback(rec)
    await _toggle(hass, "on")
    assert rec.transitions == []
    assert provider.status is ACTIVE


async def test_refresh_never_awaits_callbacks(hass: HomeAssistant, setup_plugin, attach) -> None:
    """A transition found by async_refresh_state (missed state event) is not
    awaited by the refresh: a core holding its own lock cannot deadlock."""
    hass.states.async_set(HEATER, "on")
    entry = await setup_plugin([HEATER])
    detector = entry.runtime_data
    provider, _ = await attach()
    core_lock = asyncio.Lock()
    received = []

    async def core_callback(transition) -> None:
        async with core_lock:
            received.append(transition.new_status)

    provider.register_transition_callback(core_callback)
    # Simulate a missed state event: stop following the source, change it.
    detector._unsubscribe()  # pylint: disable=protected-access
    hass.states.async_set(HEATER, "off")
    await hass.async_block_till_done()
    assert provider.status is ACTIVE

    async with core_lock:
        status = await asyncio.wait_for(provider.async_refresh_state(), 1)
        assert status is INACTIVE
        assert received == []
    await hass.async_block_till_done()
    assert received == [INACTIVE]


async def test_refresh_returns_confirmed_not_raw(hass: HomeAssistant, setup_plugin, attach) -> None:
    """A running timer is never visible through async_refresh_state."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], activation=60)
    provider, _ = await attach()
    await _toggle(hass, "on")
    assert await provider.async_refresh_state() is INACTIVE


async def test_post_init_and_restore_state_are_noops(hass: HomeAssistant, setup_plugin, attach) -> None:
    """post_init and restore_state do not change the provider."""
    hass.states.async_set(HEATER, "on")
    await setup_plugin([HEATER])
    provider, rec = await attach()
    provider.post_init({"anything": 1})
    provider.restore_state(object())
    assert provider.status is ACTIVE
    assert rec.transitions == []


async def test_policy_do_not_turn_off(hass: HomeAssistant, setup_plugin, attach) -> None:
    """The configured policy is exposed and carried by transitions."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], physical_stop_policy="do_not_turn_off")
    provider, rec = await attach()
    assert provider.physical_stop_policy is InhibitionStopPolicy.DO_NOT_TURN_OFF
    await _toggle(hass, "on")
    assert rec.transitions[0].stop_policy is InhibitionStopPolicy.DO_NOT_TURN_OFF


async def test_policy_invalid_falls_back_to_turn_off(hass: HomeAssistant, setup_plugin, attach) -> None:
    """An invalid stored policy is reported as TURN_OFF."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER], physical_stop_policy="whatever")
    provider, _ = await attach()
    assert provider.physical_stop_policy is InhibitionStopPolicy.TURN_OFF


async def test_two_providers_have_independent_sequences(hass: HomeAssistant, setup_plugin, attach) -> None:
    """A second provider for the same VTherm (e.g. core reload) has its own sequence."""
    hass.states.async_set(HEATER, "off")
    await setup_plugin([HEATER])
    first, rec1 = await attach()
    await _toggle(hass, "on")
    second, rec2 = await attach()
    await _toggle(hass, "off")
    assert [t.sequence for t in rec1.transitions] == [1, 2]
    assert [t.sequence for t in rec2.transitions] == [1]
