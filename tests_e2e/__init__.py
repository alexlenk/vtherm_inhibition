"""End-to-end tests: the real Versatile Thermostat core + this plugin.

They load BOTH custom integrations in the Home Assistant test harness
(pytest-homeassistant-custom-component). The core is taken from the checkout
pointed to by the ``VTHERM_CORE_PATH`` environment variable (a
``versatile_thermostat`` repository with inhibition support, issue #2087);
without it every module is skipped at collection.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _core_path() -> Path | None:
    value = os.environ.get("VTHERM_CORE_PATH", "")
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if not (path / "custom_components" / "versatile_thermostat" / "manifest.json").is_file():
        return None
    return path


CORE_PATH = _core_path()
SKIP_REASON = (
    None
    if CORE_PATH is not None
    else "set VTHERM_CORE_PATH to a versatile_thermostat checkout to run the end-to-end tests"
)

if CORE_PATH is not None:
    # The core's custom_components joins this repository's one (namespace package)
    for _path in (str(CORE_PATH), str(Path(__file__).resolve().parent.parent)):
        if _path not in sys.path:
            sys.path.insert(0, _path)
