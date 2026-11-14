"""Persistent per-group observation phases; speech remains a model decision."""

import json
import logging
import math
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("astrbot")


class ReadAirSchedule:
    """Keep independently sampled phase deadlines and durable read cursors."""

    PHASES = ("large", "medium", "small", "small", "medium")
    DEFAULTS = {
        "small": (300, 900),
        "medium": (900, 1800),
        "large": (1800, 3600),
        "night": (6000, 8400),
    }

    def __init__(self, config, path: Path, rng=None):
        self.config = config
        self.path = path
        self.random = rng or random.Random()
        self.groups = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for umo, row in raw.get("groups", {}).items():
                    if (
                        isinstance(row, dict)
                        and type(row.get("index")) is int
                        and 0 <= row["index"] < len(self.PHASES)
                        and all(
                            isinstance(row.get(key, 0), (int, float))
                            and math.isfinite(row.get(key, 0))
                            and row.get(key, 0) >= 0
                            for key in ("due", "cursor", "last_sent")
                        )
                    ):
                        self.groups[str(umo)] = row
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning("[mem_chat] Could not restore read-air phases: %s", exc)

    def save(self):
        """Atomically persist phase deadlines without storing message bodies."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "groups": self.groups}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def night(self, now):
        """Return whether Shanghai local time is in the configured night window.

        Args:
            now: Wall-clock timestamp.

        Returns:
            Whether the overnight observation range applies.
        """
        hour = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).hour
        start = int(self.config.get("read_air_night_start_hour", 1)) % 24
        end = int(self.config.get("read_air_night_end_hour", 8)) % 24
        if start == end:
            return False
        return start <= hour < end if start < end else hour >= start or hour < end

    def ensure(self, umo, *, now=None, heat=0):
        """Read a stable phase or re-sample once when the day/night mode changes.

        Args:
            umo: Group conversation scope.
            now: Wall-clock timestamp, injectable for tests.
            heat: Recent independent utterances per minute.

        Returns:
            Mutable phase record whose deadline is unchanged by new arrivals.
        """
        now = time.time() if now is None else now
        row = self.groups.get(umo)
        if row is None:
            row = {"index": 0, "cursor": max(0, now - 1), "last_sent": 0, "seen": []}
            self.groups[umo] = row
        if not row.get("due") or row.get("night") != self.night(now):
            self.schedule(umo, row["index"], now=now, heat=heat)
        return row

    def schedule(self, umo, index, *, now=None, heat=0):
        """Sample one deadline for a phase and retain it across ordinary arrivals.

        Args:
            umo: Group conversation scope.
            index: Position in large, medium, small, small, medium.
            now: Wall-clock timestamp.
            heat: Recent utterances per minute, used only to bias the time draw.
        """
        now = time.time() if now is None else now
        row = self.groups[umo]
        night = self.night(now)
        phase = "night" if night else self.PHASES[index]
        configured = self.config.get("read_air_periods", {})
        bounds = (
            configured.get(phase, self.DEFAULTS[phase])
            if isinstance(configured, dict)
            else self.DEFAULTS[phase]
        )
        try:
            low, high = map(float, bounds)
            if not 1 <= low <= high <= 86400:
                raise ValueError("invalid phase range")
        except (TypeError, ValueError):
            low, high = self.DEFAULTS[phase]
        delay = self.random.uniform(low, high)
        if not night and (heat < 1 or heat >= 6):
            other = self.random.uniform(low, high)
            delay = max(delay, other) if heat < 1 else min(delay, other)
        row.update(
            index=index, night=night, phase=phase, due=now + delay, interval=delay
        )
        self.save()
        logger.info(
            "[mem_chat] Read-air phase scope=%s phase=%s index=%s wait=%.1fs due=%s",
            umo,
            phase,
            index,
            delay,
            datetime.fromtimestamp(row["due"], ZoneInfo("Asia/Shanghai")).isoformat(),
        )

    def wait(self, umo, *, now=None, heat=0):
        """Return time to the deadline or the next day/night boundary.

        Args:
            umo: Group conversation scope.
            now: Wall-clock timestamp.
            heat: Recent utterances per minute.

        Returns:
            Nonnegative sleep duration; a mode boundary does not invoke the model.
        """
        now = time.time() if now is None else now
        row = self.ensure(umo, now=now, heat=heat)
        local = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
        boundaries = []
        for key, default in (
            ("read_air_night_start_hour", 1),
            ("read_air_night_end_hour", 8),
        ):
            hour = int(self.config.get(key, default)) % 24
            boundary = local.replace(hour=hour, minute=0, second=0, microsecond=0)
            if boundary.timestamp() <= now:
                boundary += timedelta(days=1)
            boundaries.append(boundary.timestamp())
        return max(0, min(row["due"], *boundaries) - now)

    def finish(self, umo, *, sent, advance, cutoff, seen=(), now=None, heat=0):
        """Commit an observed window and choose the next phase once per turn.

        Args:
            umo: Group conversation scope.
            sent: Whether this turn actually delivered visible content.
            advance: Whether a silent read-air check advances the phase sequence.
            cutoff: Timestamp of the frozen message snapshot.
            seen: IDs observed in that snapshot, used for timestamp overlap.
            now: Time after all reply segments have completed.
            heat: Recent utterances per minute.
        """
        now = time.time() if now is None else now
        row = self.ensure(umo, now=now, heat=heat)
        row["cursor"] = max(row.get("cursor", 0), cutoff - 2)
        row["seen"] = list(dict.fromkeys([*row.get("seen", []), *map(str, seen)]))[
            -256:
        ]
        if sent:
            row["last_sent"] = now
            self.schedule(umo, 4, now=now, heat=heat)
        elif advance:
            self.schedule(
                umo, (row["index"] + 1) % len(self.PHASES), now=now, heat=heat
            )
        else:
            self.save()

    def resume_after_focus(self, umo, *, now=None, heat=0):
        """Start a fresh small phase when focus expires.

        Args:
            umo: Group conversation scope.
            now: Current wall-clock timestamp.
            heat: Recent utterances per minute.
        """
        now = time.time() if now is None else now
        self.ensure(umo, now=now, heat=heat)
        self.schedule(umo, 3, now=now, heat=heat)
