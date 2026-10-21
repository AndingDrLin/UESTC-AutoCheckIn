"""Pure scheduling helpers (easy to unit test without a database)."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def window_bounds(day: date, start: time, end: time, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, start, tzinfo=tz),
        datetime.combine(day, end, tzinfo=tz),
    )


def random_times_in_window(
    start: datetime,
    end: datetime,
    count: int,
    min_gap_sec: int = 30,
    rng: random.Random | None = None,
) -> list[datetime]:
    """Return ``count`` sorted random instants inside ``[start, end]``.

    Consecutive instants are at least ``min_gap_sec`` apart when the window is
    large enough; otherwise the gap is relaxed and the times are spread evenly.
    """
    rng = rng or random.SystemRandom()
    if count <= 0:
        return []
    total = (end - start).total_seconds()
    if total <= 0:
        return []

    gap = max(0, min_gap_sec)
    if count > 1 and (count - 1) * gap > total:
        gap = total / (count - 1)

    points: list[float] | None = None
    for _ in range(500):
        sample = sorted(rng.uniform(0, total) for _ in range(count))
        if all(sample[i] - sample[i - 1] >= gap for i in range(1, count)):
            points = sample
            break

    if points is None:
        # Dense fallback: one random instant per equal slice, keeping order.
        slot = total / count
        points = sorted(
            i * slot + rng.uniform(slot * 0.15, slot * 0.85) for i in range(count)
        )

    points = [min(max(0.0, p), total) for p in points]
    return [start + timedelta(seconds=p) for p in points]


def next_retry_epoch(
    now_epoch: int,
    window_end_epoch: int,
    interval_sec: int,
    attempt: int,
    rng: random.Random | None = None,
) -> int | None:
    """Pick the next retry instant, or ``None`` when the window is exhausted."""
    rng = rng or random.SystemRandom()
    # Exponential-ish backoff capped by the configured interval: 1x, 1.5x, 2x.
    factor = min(2.0, 1.0 + 0.5 * max(0, attempt - 1))
    delay = int(interval_sec * factor)
    jitter = rng.randint(-15, 15)
    candidate = now_epoch + max(30, delay + jitter)
    if candidate >= window_end_epoch - 20:
        return None
    return candidate


def minutes_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)
