"""Pure scheduling helpers."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.planning import (
    minutes_between,
    next_retry_epoch,
    random_times_in_window,
    window_bounds,
)

TZ = ZoneInfo("Asia/Shanghai")


def test_window_bounds_respects_timezone():
    start, end = window_bounds(date(2026, 10, 21), time(21, 30), time(23, 0), TZ)
    assert start.tzinfo is not None
    assert start.hour == 21 and start.minute == 30
    assert end.hour == 23 and end.minute == 0
    assert minutes_between(start, end) == 90


def test_random_times_are_sorted_inside_window_with_gap():
    start, end = window_bounds(date(2026, 10, 21), time(21, 30), time(23, 0), TZ)
    times = random_times_in_window(start, end, 6, min_gap_sec=30, rng=random.Random(42))

    assert len(times) == 6
    assert times == sorted(times)
    assert all(start <= item <= end for item in times)
    assert all(
        (later - earlier).total_seconds() >= 30
        for earlier, later in zip(times, times[1:])
    )


def test_random_times_handles_tight_windows():
    start = datetime(2026, 10, 21, 21, 30, tzinfo=TZ)
    end = start + timedelta(seconds=50)
    times = random_times_in_window(start, end, 5, min_gap_sec=30)
    assert len(times) == 5
    assert times == sorted(times)
    assert all(start <= item <= end for item in times)


def test_next_retry_backoff_and_window_end():
    now = 1_000_000
    end = now + 3_600

    first = next_retry_epoch(now, end, 180, 1, rng=random.Random(0))
    assert first is not None
    assert now + 165 <= first <= now + 195

    later = next_retry_epoch(first, end, 180, 4, rng=random.Random(0))
    assert later is not None and later > first

    assert next_retry_epoch(end - 30, end, 180, 1) is None
