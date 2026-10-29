"""Load the project facility catalog."""

import json
from pathlib import Path
from typing import Any


_CATALOG_PATH = Path(__file__).with_name("facility_catalog.json")


def load_catalog() -> dict[str, Any]:
    """Return the versioned catalog used by the checkup workflow."""
    with _CATALOG_PATH.open(encoding="utf-8") as source:
        return json.load(source)
