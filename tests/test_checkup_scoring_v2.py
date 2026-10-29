"""Access-evidence verdicts (kept from score-method v2; scoring is now v3).

``count``/``known_count`` remain fact fields (an incomplete search still reports
``count=None``). The verdict says whether centre-to-facility routes confirmed
reachability; it never changes the v3 presence-count score.
"""

from app.modules.checkup.service import _access_status


def test_reachable_evidence_always_wins() -> None:
    assert _access_status(reachable=2, unresolved=5, count=None, observed=["route"]) == "reachable"


def test_complete_search_without_facility_scores_not_found() -> None:
    assert _access_status(reachable=0, unresolved=0, count=0, observed=[]) == "not_found"


def test_incomplete_search_is_unverified_not_unknown() -> None:
    assert _access_status(reachable=0, unresolved=0, count=None, observed=[]) == "unverified"


def test_unmeasured_routes_are_unverified() -> None:
    assert _access_status(reachable=0, unresolved=3, count=5, observed=["route"]) == "unverified"


def test_facilities_present_but_unreachable() -> None:
    assert _access_status(reachable=0, unresolved=0, count=2, observed=["route"]) == "unreachable"


def test_v2_never_leaves_a_category_without_a_verdict() -> None:
    for count in (None, 0, 3):
        for unresolved in (0, 2):
            for observed in ([], ["route"]):
                status = _access_status(reachable=0, unresolved=unresolved, count=count, observed=observed)
                assert status in {"not_found", "unreachable", "unverified"}
