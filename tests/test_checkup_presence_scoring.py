"""Score method v3: presence-count tiers are independent from route success."""

import math

from app.modules.checkup import scoring
from app.modules.checkup.catalog import load_catalog


def _quantize(value: float) -> float:
    return min(100.0, max(0.0, math.floor(value / 2.5 + 0.5) * 2.5))


def test_profiles_cover_all_45_categories() -> None:
    catalog = load_catalog()
    config = scoring.load_profiles()
    ids = {item["id"] for item in catalog["facilities"]}
    assert set(config["facilities"]) == ids
    counts = {"A": 0, "B": 0, "C": 0}
    for profile in config["facilities"].values():
        counts[profile] += 1
    assert counts == {"A": 12, "B": 20, "C": 13}


def test_tier_templates_are_monotonic_and_match_the_frozen_bands() -> None:
    expected = {
        "A": [(0, 0), (1, 80), (2, 90), (3, 100)],
        "B": [(0, 0), (1, 60), (2, 75), (3, 90), (5, 100)],
        "C": [(0, 0), (1, 50), (2, 65), (4, 80), (8, 90), (16, 100)],
    }
    for profile, bands in expected.items():
        previous = -1.0
        for min_count, score in bands:
            value, _, _ = scoring.tier_for(profile, min_count)
            assert value == score
            assert value >= previous
            previous = value
        top = bands[-1][0]
        assert scoring.tier_for(profile, top + 100)[0] == bands[-1][1]


def test_score_uses_count_basis_not_route_success() -> None:
    complete = scoring.score_category("primary_school", "complete", 3, 3)
    assert (complete.profile, complete.score, complete.status) == ("B", 90, "scored")
    assert complete.band == "3 处 → 90 分"
    assert complete.next_band == "5 处 → 100 分"

    partial = scoring.score_category("primary_school", "partial", None, 2)
    assert (partial.score, partial.status, partial.basis_count) == (75, "provisional", 2)

    failed_known = scoring.score_category("primary_school", "failed", None, 2)
    assert (failed_known.score, failed_known.status) == (75, "provisional")

    failed_empty = scoring.score_category("primary_school", "failed", None, 0)
    assert (failed_empty.score, failed_empty.status, failed_empty.basis_count) == (None, "unavailable", None)


def test_complete_zero_count_is_a_real_zero_not_unavailable() -> None:
    score = scoring.score_category("fire_station", "complete", 0, 0)
    assert score.score == 0
    assert score.status == "scored"
    assert score.band == "0 处 → 0 分"
    assert score.next_band == "1 处 → 80 分"


def test_tongji_fixed_sample_recalculates_to_52_5() -> None:
    """Same counts as the handoff recalc; only the scoring rule changes."""
    counts_by_name = {
        "婴幼儿托育": 0, "幼儿园": 2, "小学": 3, "中学": 2, "儿童托管与课后服务": 0,
        "教育培训": 115, "社区卫生服务中心": 0, "社区卫生服务站": 0, "诊所与门诊部": 2,
        "医院": 1, "药店": 2, "康复服务": 1, "心理健康服务": 1, "养老服务设施": 0,
        "老年人日间照料": 0, "老年助餐": 0, "家政与居家照护": 1, "助残与康复辅具服务": 0,
        "菜市场与农贸市场": 0, "超市": 2, "便利店": 8, "社区食堂": 11, "餐饮服务": 35,
        "邮政与快递服务": 0, "银行与金融服务点": 15, "理发服务": 2, "洗衣服务": 3,
        "维修服务": 0, "图书馆与阅览空间": 2, "文化活动设施": 1, "体育场馆": 6,
        "球类运动场地": 3, "户外健身设施": 2, "公园与公共绿地": 1, "儿童游憩场地": 0,
        "公共厕所": 7, "公交站点": 9, "轨道交通站点": 6, "公共停车场": 36,
        "电动汽车充电设施": 2, "再生资源回收点": 0, "社区综合服务中心": 0,
        "居民委员会": 12, "警务服务点": 2, "消防服务点": 0,
    }
    catalog = load_catalog()
    assert {item["name_zh"] for item in catalog["facilities"]} == set(counts_by_name)
    group_values: dict[str, list[float]] = {}
    for facility in catalog["facilities"]:
        count = counts_by_name[facility["name_zh"]]
        score = scoring.score_category(facility["id"], "complete", count, count)
        assert score.score is not None
        group_values.setdefault(facility["group_id"], []).append(score.score)
    group_averages = [sum(values) / len(values) for values in group_values.values()]
    assert len(group_averages) == 7
    total = _quantize(sum(group_averages) / 7)
    assert total == 52.5
