"""Load installation values without putting credentials into source files."""

import json
import os
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")


def read_env(path):
    """Read a literal dotenv file; shell expressions are never evaluated."""
    values = {}
    if path.exists():
        for number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), 1
        ):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                raise ValueError(f"Invalid environment entry on line {number}")
            value = value.strip()
            if value.startswith('"'):
                value = json.loads(value)
            elif len(value) >= 2 and value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if not isinstance(value, str):
                raise ValueError(f"Environment entry {key} must be a string")
            values[key] = value
    return values


@lru_cache(maxsize=1)
def load_values():
    """Resolve file values, environment overrides and portable default paths."""
    specs = json.loads((ROOT / "config/variables.json").read_text(encoding="utf-8"))
    values = {name: spec["default"] for name, spec in specs.items()}
    values.update(read_env(ROOT / ".env"))
    values.update({key: value for key, value in os.environ.items() if key in specs})
    values.update(ASTRBOT_ROOT=str(ROOT), USER_HOME=str(Path.home()))
    for _ in range(8):
        values = {
            key: MARKER.sub(lambda match: values.get(match[1], ""), value)
            for key, value in values.items()
        }
    return values


def resolve(text):
    """Replace personal installation constants in the exported Python source."""
    values = load_values()
    return MARKER.sub(lambda match: values.get(match[1], ""), text)
