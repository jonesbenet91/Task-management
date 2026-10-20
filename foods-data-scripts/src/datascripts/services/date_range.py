"""Shared parsing of CLI date/time window options (``--since`` / ``--until``).

Commands accept three interchangeable forms for a window boundary:

* a relative ISO8601 duration -- ``P1D``, ``P2W``, ``P1M``, ``P1Y``
* a plain date -- ``2026-06-01``
* an absolute ISO8601 timestamp -- ``2026-06-01T09:30:00Z``

Everything is normalised to a timezone-aware UTC datetime so that downstream
comparisons, API query strings, and cache keys are always consistent.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import click

# ISO8601 duration subset used for relative values (e.g. P1D, P2W, P1M, P1Y).
DURATION_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$", re.IGNORECASE
)

# Wire format for absolute timestamps, and the format used for every cache key.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_timestamp(value: datetime) -> str:
    """Render a datetime as a UTC ISO8601 timestamp.

    Args:
        value: Datetime to format (naive values are assumed to be UTC).

    Returns:
        Timestamp string such as ``2026-08-25T13:00:00Z``.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_datetime_value(
    value: Optional[str],
    option_name: str = "--since",
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Parse a CLI date/time value into a timezone-aware UTC datetime.

    Accepts relative ISO8601 durations (``P1D``, ``P2W``, ``P1M``, ``P1Y``), plain
    dates (``2026-06-01``) and absolute ISO8601 timestamps with or without an
    explicit offset. Values without an offset are treated as UTC so that every
    comparison downstream is timezone-aware.

    Args:
        value: Raw option value.
        option_name: Option name used in error messages.
        now: Reference point for relative durations (defaults to the current time).

    Returns:
        Timezone-aware UTC datetime, or None when no value was supplied.

    Raises:
        click.BadParameter: If the value cannot be interpreted as a date/time.
    """
    if value is None:
        return None

    raw = value.strip()
    if not raw:
        return None

    reference = now or datetime.now(timezone.utc)

    match = DURATION_RE.match(raw)
    if match and any(match.groups()):
        years, months, weeks, days = (int(g) if g else 0 for g in match.groups())
        total_days = years * 365 + months * 30 + weeks * 7 + days
        return reference - timedelta(days=total_days)

    candidate = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise click.BadParameter(
            f"{option_name} value '{value}' is not a valid ISO8601 date/time or "
            f"duration. Use e.g. 2026-06-01, 2026-06-01T09:30:00Z, or P1M.",
            param_hint=option_name,
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
