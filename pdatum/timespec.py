"""
Times, as a person or an agent would write them, turned into the epoch
seconds the API takes.

    1727000000              epoch seconds, as-is
    2026-09-01              midnight UTC that day
    2026-09-01T12:30:00Z    an ISO timestamp; no offset means UTC
    7d, 12h, 30m            that long ago
"""

import re
import time
from datetime import datetime, timezone

_RELATIVE = re.compile(r"^(\d+)\s*([dhm])$")
_UNITS = {"d": 86400, "h": 3600, "m": 60}


def parse(value, now=None):
    text = str(value).strip()
    if not text:
        raise ValueError("a time is required")

    if text.isdigit():
        return int(text)

    match = _RELATIVE.match(text.lower())
    if match:
        now = time.time() if now is None else now
        return int(now - int(match.group(1)) * _UNITS[match.group(2)])

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"{value!r} is not a time. Use epoch seconds, a date (2026-09-01), "
            f"an ISO timestamp, or an age like 7d, 12h, 30m."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def show(epoch):
    """An epoch as a UTC date and time, for people; '-' for none."""
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
