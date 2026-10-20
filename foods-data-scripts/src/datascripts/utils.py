"""Shared utility functions for data-scripts commands."""

from typing import Any


def safe_float(val: Any, default: float = 0.0) -> float:
    """Convert a value to float, returning default on failure."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default
