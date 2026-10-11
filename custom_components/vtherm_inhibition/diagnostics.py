"""Config entry diagnostics (entity IDs and states only, no secret)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import InhibitionConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: InhibitionConfigEntry) -> dict[str, Any]:
    """Return the entry configuration and the detector state."""
    detector = entry.runtime_data
    return {
        "entry": {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
        "detector": detector.diagnostics(),
        "attached_providers": len(detector.providers),
        "created_providers": detector.created_providers,
        "provider_sequences": [provider.sequence for provider in detector.providers],
    }
