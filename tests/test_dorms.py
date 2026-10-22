"""Unit tests for dorm-building naming and coordinate jitter."""
from __future__ import annotations

import math
import random

from app.uestc.dorms import (
    DEFAULT_ADDRESS,
    JITTER_RADIUS_M,
    SEED_BUILDINGS,
    jitter,
    normalize_building,
    offset_meters,
)


def test_normalize_building_common_spellings():
    assert normalize_building("本科13栋") == "本科13栋"
    assert normalize_building("13栋") == "本科13栋"
    assert normalize_building("13 栋") == "本科13栋"
    assert normalize_building("本科１３栋") == "本科13栋"
    assert normalize_building("学知苑 5 栋") == "本科5栋"
    assert normalize_building("本科2组团") == "本科2组团"
    assert normalize_building("2组团") == "本科2组团"
    assert normalize_building("本科 3 号楼") == "本科3栋"


def test_normalize_building_rejects_empty():
    assert normalize_building("") == ""
    assert normalize_building("   ") == ""
    assert normalize_building("宿舍") == ""


def test_seed_buildings_are_unique_and_plausible():
    names = [name for name, _, _, _ in SEED_BUILDINGS]
    assert len(names) == len(set(names))
    assert "本科13栋" in names
    assert "本科4栋" in names
    assert "本科2组团" in names
    assert "本科4组团" in names
    assert "本科3组团" not in names
    for name, latitude, longitude, source in SEED_BUILDINGS:
        assert normalize_building(name) == name
        assert 30.7 < latitude < 30.8
        assert 103.9 < longitude < 104.0
        assert source


def test_jitter_stays_within_radius():
    rng = random.Random(20261022)
    base = (30.753736, 103.934585)
    points = [jitter(*base, rng=rng) for _ in range(500)]
    assert len(set(points)) > 400
    for point in points:
        assert offset_meters(base, point) <= JITTER_RADIUS_M + 0.5


def test_jitter_is_rounded_to_six_decimals():
    point = jitter(30.753736, 103.934585, rng=random.Random(7))
    assert round(point[0], 6) == point[0]
    assert round(point[1], 6) == point[1]


def test_offset_meters_matches_haversine_scale():
    base = (30.753736, 103.934585)
    # 纬度方向 0.001 度约 110.6 米。
    assert abs(offset_meters(base, (30.754736, 103.934585)) - 110.6) < 1.0
    assert offset_meters(base, base) == 0
    assert DEFAULT_ADDRESS.startswith("四川省")
    assert not math.isnan(offset_meters(base, (30.75, 103.93)))
