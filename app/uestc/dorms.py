"""宿舍楼坐标基准表与坐标扰动。

新增用户时只填「本科X栋」，坐标由基准表匹配，再加一点随机扰动：
同楼栋的多个人位置略有差异，但都留在打卡点半径内。
"""
from __future__ import annotations

import math
import random
import re
from typing import Any

# 扰动半径（米）。学校打卡点半径 130–150 m，25 m 足够分散又不会越界。
JITTER_RADIUS_M = 25.0
DEFAULT_ADDRESS = "四川省成都市郫都区"

_M_PER_DEG_LAT = 110_574.0
_M_PER_DEG_LNG_EQ = 111_320.0

# (楼栋, 纬度, 经度, 来源)。来源仅用于设置页展示。
SEED_BUILDINGS: tuple[tuple[str, float, float, str], ...] = (
    ("本科13栋", 30.753736, 103.934585, "林宇嘉的坐标"),
    ("本科4栋", 30.753960, 103.931514, "朱子祎的坐标"),
    ("本科2组团", 30.753786, 103.932332, "学校打卡点"),
    ("本科4组团", 30.753818, 103.934258, "学校打卡点"),
)

_FULLWIDTH = str.maketrans("０１２３４５６７８９　", "0123456789 ")
_NAME_RE = re.compile(r"(?:本科)?(\d{1,2})(栋|舍|号楼|组团|苑)?$")


def normalize_building(name: str) -> str:
    """把「4栋」「本科４栋」「 本科 4 栋 」「学知苑 5 栋」统一成「本科4栋」。

    不符合「本科N栋 / 本科N组团」命名习惯的输入返回空字符串。
    """
    text = (name or "").translate(_FULLWIDTH).strip()
    text = "".join(text.split())
    if not text:
        return ""
    match = _NAME_RE.search(text)
    if not match:
        return ""
    number = int(match.group(1))
    kind = match.group(2)
    if kind == "组团":
        return f"本科{number}组团"
    return f"本科{number}栋"


def sort_key(name: str) -> tuple[int, int, str]:
    """楼栋列表排序：先「栋」后「组团」，同类型按编号。"""
    match = _NAME_RE.search((name or "").translate(_FULLWIDTH))
    if not match:
        return (2, 0, name or "")
    kind = 1 if match.group(2) == "组团" else 0
    return (kind, int(match.group(1)), name or "")


def jitter(
    latitude: float,
    longitude: float,
    radius_m: float = JITTER_RADIUS_M,
    rng: Any | None = None,
) -> tuple[float, float]:
    """在半径 radius_m 的圆内随机取一点（米制均匀分布）。"""
    rand = rng or random
    distance = radius_m * math.sqrt(rand.random())
    bearing = rand.random() * 2 * math.pi
    north = distance * math.cos(bearing)
    east = distance * math.sin(bearing)
    lat = latitude + north / _M_PER_DEG_LAT
    lng = longitude + east / (_M_PER_DEG_LNG_EQ * math.cos(math.radians(latitude)))
    return round(lat, 6), round(lng, 6)


def offset_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点近似距离（米），用于测试与展示。"""
    north = (a[0] - b[0]) * _M_PER_DEG_LAT
    east = (a[1] - b[1]) * _M_PER_DEG_LNG_EQ * math.cos(math.radians(a[0]))
    return math.hypot(north, east)
