"""Presence-count tier scoring (score method v3).

The score reflects how many distinct facilities of one category were found in
the analysis range. Route success and coverage results are reported separately
and never change this score. Three simple tier templates avoid 45 bespoke
formulas; the profile assignment and thresholds live in a versioned JSON file
so a rule change is explicit rather than scattered through the service.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

SCORING_VERSION = "presence-count-v3"
SCORE_METHOD_TEXT = (
    "设施配置参考分 v3：按本次分析范围内去重且类别匹配的独立设施数量分档"
    "（A 节点型 / B 社区型 / C 高频网点）；路线是否成功、覆盖结果不参与数量分。"
    "本次检索完成且未发现计 0 分；检索未完成但已有已知点位时按已知数量暂估；"
    "检索失败且没有任何已知点位时暂不计分。组内等权、所选组等权，2.5 分取最近档；\n"
    "只选部分类别时的分数不能与全 45 类总分直接比较。"
)

_PROFILES_PATH = Path(__file__).with_name("scoring_profiles.json")
_CATALOG_PATH = Path(__file__).with_name("facility_catalog.json")


@dataclass(frozen=True, slots=True)
class CategoryScore:
    facility_id: str
    profile: str
    score: float | None
    status: str  # scored | provisional | unavailable
    basis_count: int | None
    band: str | None
    next_band: str | None


def _format_score(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@lru_cache(maxsize=1)
def load_profiles() -> dict[str, Any]:
    with _PROFILES_PATH.open(encoding="utf-8") as source:
        config = json.load(source)
    profiles = config["profiles"]
    if set(profiles) != {"A", "B", "C"}:
        raise RuntimeError("评分模板必须恰好包含 A/B/C 三档。")
    for profile, payload in profiles.items():
        bands = payload["bands"]
        if not bands or bands[0]["min_count"] != 0:
            raise RuntimeError(f"评分模板 {profile} 必须以 0 处为起始档。")
        thresholds = [band["min_count"] for band in bands]
        if thresholds != sorted(thresholds) or len(set(thresholds)) != len(thresholds):
            raise RuntimeError(f"评分模板 {profile} 的阈值必须严格递增。")
    facilities = config["facilities"]
    with _CATALOG_PATH.open(encoding="utf-8") as source:
        catalog_ids = {item["id"] for item in json.load(source)["facilities"]}
    if set(facilities) != catalog_ids:
        missing = sorted(catalog_ids - set(facilities))
        unknown = sorted(set(facilities) - catalog_ids)
        raise RuntimeError(f"评分模板与设施目录不一致：缺少 {missing}，多出 {unknown}")
    return config


def profile_for(facility_id: str) -> str:
    try:
        return load_profiles()["facilities"][facility_id]
    except KeyError as exc:
        raise KeyError(f"设施 {facility_id} 没有评分配置。") from exc


def tier_for(profile: str, basis_count: int) -> tuple[float, str, str | None]:
    """Return (score, current band label, next band label)."""
    bands = load_profiles()["profiles"][profile]["bands"]
    chosen = bands[0]
    next_band = None
    for index, band in enumerate(bands):
        if band["min_count"] <= basis_count:
            chosen = band
            next_band = bands[index + 1] if index + 1 < len(bands) else None
    score = float(chosen["score"])
    current_label = f"{chosen['min_count']} 处 → {_format_score(chosen['score'])} 分"
    next_label = (
        f"{next_band['min_count']} 处 → {_format_score(next_band['score'])} 分"
        if next_band is not None
        else None
    )
    return score, current_label, next_label


def score_category(
    facility_id: str,
    search_status: str,
    count: int | None,
    known_count: int,
) -> CategoryScore:
    """Score one category from the count basis, never from route success.

    - complete search (``count`` is not None): exact count, status ``scored``;
    - incomplete/failed search with known points: ``provisional`` on known count;
    - no usable count at all: ``unavailable`` and score ``None``.
    """
    profile = profile_for(facility_id)
    if count is not None:
        basis = count
        status = "scored"
    elif known_count > 0:
        basis = known_count
        status = "provisional"
    else:
        return CategoryScore(
            facility_id=facility_id,
            profile=profile,
            score=None,
            status="unavailable",
            basis_count=None,
            band=None,
            next_band=None,
        )
    if basis < 0:
        raise ValueError("设施数量不能为负。")
    score, band, next_band = tier_for(profile, basis)
    return CategoryScore(
        facility_id=facility_id,
        profile=profile,
        score=score,
        status=status,
        basis_count=basis,
        band=band,
        next_band=next_band,
    )
