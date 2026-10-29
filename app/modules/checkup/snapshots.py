"""Validate administrator-registered stages before reusing any provider result."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from shapely.geometry import Point, shape

from app.modules.checkup.catalog import _CATALOG_PATH
from app.schemas.blindspot import minimum_poi_search_radius_m
from app.schemas.checkup import CheckupPOI
from app.schemas.common import CenterPoint, WarningItem
from app.schemas.isochrone import IsochroneResult
from app.schemas.location import LocationRequest, LocationResult


def _require(condition):
    if not condition:
        raise ValueError("Staged evidence does not match its registration")


def _captured_at(payload):
    value = payload.get("captured_at")
    if value is not None:
        _require(isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None)
    return value


def load_stages(manifest_path, location_input, catalog):
    """Paths come only from server configuration; no API upload/import path exists.

    A missing timestamp stays unknown, including categories carried over by resume.
    SHA256 pins bytes, while the local administrator attests their live source.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_bytes())
    original = LocationRequest(schema_version="1.0", input=manifest["input"]).input.model_dump(mode="json")
    _require(original == manifest["input"])
    fingerprint = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    _require(manifest["source"] == "baidu-live-staged" and manifest["input_sha256"] == fingerprint)
    _require(manifest["catalog_version"] == catalog["version"]
             and manifest["catalog_sha256"] == hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest())
    payloads, provenance = {}, {}
    for stage in ("gate1", "gate2"):
        entry = manifest[stage]
        path = (manifest_path.parent / entry["path"]).resolve()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        _require(entry["sha256"] == digest)
        payloads[stage] = json.loads(raw)
        provenance[stage] = {"file": entry["path"], "sha256": digest,
                             "captured_at": _captured_at(payloads[stage])}
    gate1, gate2 = payloads["gate1"], payloads["gate2"]
    location = LocationResult.model_validate(gate1["location"]).root
    iso = IsochroneResult.model_validate(gate1["isochrone"]).root
    _require(location.ok and iso.ok)
    center = location.data.center
    _require(iso.data.center == center and iso.data.time_limit_s == 900)
    # A registered coordinate input itself must also identify this verified center.
    if original["type"] == "coordinate":
        _require(CenterPoint.model_validate({k: original[k] for k in ("lng", "lat", "crs")}) == center)
    requested = location_input.model_dump(mode="json")
    _require(requested == original if requested["type"] == "address" else
             CenterPoint.model_validate({k: requested[k] for k in ("lng", "lat", "crs")}) == center)
    polygon = shape(iso.data.geometry.model_dump())
    _require(polygon.is_valid and not polygon.is_empty and polygon.covers(Point(center.lng, center.lat)))
    _require(CenterPoint.model_validate(gate2["center"]) == center
             and gate2["catalog_version"] == catalog["version"]
             and gate2["gate1_file"] == Path(manifest["gate1"]["path"]).name)
    if "gate1_sha256" in gate2:
        _require(gate2["gate1_sha256"] == provenance["gate1"]["sha256"])
    buffer = max([1000] + [f.get("service_distance_m") or 0 for f in catalog["facilities"]])
    radius = minimum_poi_search_radius_m(center, iso.data.geometry, buffer)
    _require(type(gate2["search_radius_m"]) is int and gate2["search_radius_m"] == radius)
    definitions = {f["id"]: f for f in catalog["facilities"]}
    supplied, positions = {}, {}
    for row in gate2["categories"]:
        fid = row["facility_id"]
        _require(fid in definitions and fid not in supplied
                 and row["search_status"] in {"complete", "partial", "failed"})
        supplied[fid] = row
    searches, completeness = [], []
    for fid, definition in definitions.items():
        row = supplied.get(fid)
        status = row["search_status"] if row is not None else "failed"
        unique = {}
        for raw in row["pois"] if row is not None else []:
            item = CheckupPOI.model_validate({**raw, "in_isochrone": False})
            _require(item.facility_id == fid and bool(item.poi_id) and bool(item.name))
            _require(item.poi_id not in positions or positions[item.poi_id] == item.location)
            positions[item.poi_id] = item.location
            item.in_isochrone = polygon.covers(Point(item.location.lng, item.location.lat))
            unique.setdefault(item.poi_id, item.model_dump(mode="json"))
        pois = list(unique.values())
        known_count = sum(p["in_isochrone"] for p in pois)
        warnings = [WarningItem.model_validate(w).model_dump() for w in row.get("warnings", [])] if row else []
        if status != "complete":
            warnings.append({"code": "PARTIAL_POI_RESULTS" if status == "partial" else "POI_SEARCH_FAILED",
                             "message": f"{definition['name_zh']}阶段检索不完整或缺失，数量保持未知。"})
        searches.append({"facility_id": fid, "name_zh": definition["name_zh"],
                         "group_id": definition["group_id"], "search_status": status,
                         "count": known_count if status == "complete" else None,
                         "known_count": known_count, "pois": pois, "warnings": warnings})
        completeness.append({"facility_id": fid, "search_status": status,
                             "known_count": known_count, "captured_at": _captured_at(row) if row else None})
    provenance.update(source="baidu-live-staged", reused_stages=["location", "isochrone", "poi"],
                      catalog_sha256=manifest["catalog_sha256"], input_sha256=fingerprint,
                      categories=completeness)
    return location, iso, searches, provenance
