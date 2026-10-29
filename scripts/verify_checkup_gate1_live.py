"""One bounded real check of the fixed checkup's Location -> detailed Isochrone gate.

Run only with RUN_BAIDU_GATE1=1 and a server-side BAIDU_MAP_AK in the environment.
Prints public diagnostics only; never writes the key or raw provider response.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from shapely.geometry import Point, shape


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.modules.isochrone.service import IsochroneService
from app.modules.location.service import resolve_location
from app.modules.routing.service import RoutingService
from app.providers.baidu.geocoding import BaiduGeocodingProvider
from app.providers.baidu.routing import BaiduRoutingProvider
from app.schemas.isochrone import IsochroneRequest
from app.schemas.location import LocationRequest


def main() -> int:
    if os.environ.get("RUN_BAIDU_GATE1") != "1":
        print("Skipped: set RUN_BAIDU_GATE1=1 for one real first-gate check.")
        return 2
    ak = os.environ.get("BAIDU_MAP_AK", "").strip()
    if not ak:
        print("Skipped: server-side BAIDU_MAP_AK is not configured.")
        return 2

    calls = 0
    route_pairs = 0
    last_call = 0.0
    started = time.monotonic()

    def transport(url, timeout):
        nonlocal calls, route_pairs, last_call
        path = urlsplit(url).path
        pairs = 0
        if path == "/routematrix/v2/walking":
            destinations = parse_qs(urlsplit(url).query).get("destinations", [])
            pairs = len(destinations[0].split("|")) if destinations else 0
        if calls >= 260 or route_pairs + pairs > 720 or time.monotonic() - started >= 900:
            raise TimeoutError("first-gate local request budget")
        time.sleep(max(0.0, 1.5 - (time.monotonic() - last_call)))
        last_call = time.monotonic()
        calls += 1
        route_pairs += pairs
        with urlopen(url, timeout=timeout) as response:
            return json.load(response)

    try:
        request = LocationRequest.model_validate({
            "schema_version": "1.0",
            "input": {"type": "address", "query": "同济大学四平路校区", "city": "上海市"},
        })
        location = resolve_location(request, BaiduGeocodingProvider(ak, transport=transport).geocode).root
        print("location_ok:", location.ok)
        if not location.ok:
            print("location_error:", location.error.code.value)
            return 1
        print("center:", location.data.center.model_dump())

        routing = RoutingService(
            BaiduRoutingProvider(ak, timeout_seconds=12.0, transport=transport),
            batch_size=3,
            max_retries=1,
            retry_delay_seconds=2,
        )
        result = IsochroneService.detailed(routing.route).generate(
            IsochroneRequest(schema_version="1.0", center=location.data.center, time_limit_s=900)
        ).root
        print("isochrone_ok:", result.ok)
        print("warnings:", [warning.code for warning in result.warnings])
        if result.ok:
            print("quality:", result.data.quality.model_dump(mode="json"))
            print("boundary_points:", len(result.data.boundary_points))
            polygon = shape(result.data.geometry.model_dump(mode="json"))
            center = location.data.center
            geometry_valid = polygon.is_valid and not polygon.is_empty
            center_covered = polygon.covers(Point(center.lng, center.lat))
            print("geometry_valid:", geometry_valid)
            print("center_covered:", center_covered)
            output = ROOT / "data" / "cache" / f"gate1-live-{uuid.uuid4().hex}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({
                "location": location.model_dump(mode="json"),
                "isochrone": result.model_dump(mode="json"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print("saved:", output.relative_to(ROOT))
            return 0 if geometry_valid and center_covered else 1
        else:
            print("isochrone_error:", result.error.code.value, result.error.message)
            return 1
    except Exception as exc:
        print("gate1_exception_type:", type(exc).__name__)
        return 1
    finally:
        print("provider_http_requests:", calls)
        print("route_pairs_attempted:", route_pairs)
        print("elapsed_seconds:", round(time.monotonic() - started, 1))


if __name__ == "__main__":
    raise SystemExit(main())
