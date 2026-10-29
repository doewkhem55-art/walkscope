"""Offline audit of saved grid coverage geometry; never requests map routes.

Rebuild the original clipped lattice, identify fully included cells from each
saved category geometry, and compare their union, area ratios and unknown layer.
This verifies the geometry representation, not the original route classifications.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter
from shapely.geometry import GeometryCollection, box
from shapely.ops import unary_union

from app.modules.blindspot.service import _grid_cells, _polygon_parts
from app.modules.checkup.service import _projection
from app.modules.isochrone.geometry import geometry_to_local
from app.schemas.common import CenterPoint
from app.schemas.blindspot import ClippedGeometry


def audit_report_geometry(payload: dict) -> dict:
    report = payload.get("result", payload)
    to_m, _ = _projection(CenterPoint.model_validate(report["center"]))
    parser = TypeAdapter(ClippedGeometry)

    def local(raw):
        return geometry_to_local(parser.validate_python(raw), to_m)

    domain = local(report["isochrone"]["geometry"])
    grid_size = report["quality"]["grid_size_m"]
    cells = _grid_cells(domain, grid_size)
    # Coordinate round trips contribute sub-millimetre-square slivers.
    tolerance = max(0.001, domain.area * 1e-9)
    failures = []
    if report["quality"].get("grid_cell_count") != len(cells):
        failures.append("recorded_grid_cell_count_mismatch")
    layers = {}
    for kind, key in (("blind", "blind_spots"), ("unknown", "unknown_areas")):
        grouped = {}
        for feature in report["blindspot"][key]["features"]:
            fid = feature["properties"]["facility_type"]
            grouped.setdefault(fid, []).append(local(feature["geometry"]))
        layers[kind] = {fid: unary_union(parts) for fid, parts in grouped.items()}
    rows = []
    for category in report["categories"]:
        fid = category["facility_id"]
        row = {"facility_id": fid}
        for kind in ("blind", "unknown"):
            geometry = layers[kind].get(fid, GeometryCollection())
            selected = [cell for _, cell in cells
                        if cell.intersection(geometry).area >= cell.area - tolerance]
            reconstructed = unary_union(selected) if selected else GeometryCollection()
            difference = geometry.symmetric_difference(reconstructed).area
            outside = geometry.difference(domain).area
            ratio = category.get(f"{kind}_ratio")
            ratio_error = abs(geometry.area / domain.area - ratio) if isinstance(ratio, (int, float)) else None
            polygons = _polygon_parts(geometry)
            row[kind] = {
                "clipped_grid_cells": len(selected), "polygon_parts": len(polygons),
                "rectangular_parts": sum(part.symmetric_difference(box(*part.bounds)).area <= tolerance
                                         for part in polygons),
                "area_m2": geometry.area, "grid_union_difference_m2": difference,
                "outside_domain_m2": outside, "saved_ratio_error": ratio_error,
            }
            if difference > tolerance:
                failures.append(f"{fid}:{kind}:not_a_union_of_clipped_grid_cells")
            if outside > tolerance:
                failures.append(f"{fid}:{kind}:outside_analysis_domain")
            if ratio_error is not None and ratio_error * domain.area > tolerance:
                failures.append(f"{fid}:{kind}:saved_ratio_mismatch")
        blind = layers["blind"].get(fid, GeometryCollection())
        unknown = layers["unknown"].get(fid, GeometryCollection())
        row["blind_unknown_overlap_m2"] = blind.intersection(unknown).area
        if row["blind_unknown_overlap_m2"] > tolerance:
            failures.append(f"{fid}:unknown_also_classified_as_blind")
        rows.append(row)
    grid_union_difference = unary_union([cell for _, cell in cells]).symmetric_difference(domain).area
    if grid_union_difference > tolerance:
        failures.append("grid_does_not_partition_analysis_domain")
    return {
        "passed": not failures, "report_id": report["report_id"],
        "method": "saved_geometry_clipped_grid_reconstruction",
        "limitation": "Uses saved category polygons to recover cell membership; does not replay or independently validate the original route evidence.",
        "grid_size_m": grid_size, "grid_cell_count": len(cells),
        "domain_area_m2": domain.area, "bbox_area_m2": domain.envelope.area,
        "grid_union_difference_m2": grid_union_difference, "tolerance_m2": tolerance,
        "max_grid_union_difference_m2": max(row[kind]["grid_union_difference_m2"] for row in rows for kind in ("blind", "unknown")),
        "max_blind_unknown_overlap_m2": max(row["blind_unknown_overlap_m2"] for row in rows),
        "rectangular_parts": sum(row[kind]["rectangular_parts"] for row in rows for kind in ("blind", "unknown")),
        "failures": failures, "categories": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    audit = audit_report_geometry(json.loads(args.report.read_text(encoding="utf-8")))
    result = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
        print(json.dumps({key: value for key, value in audit.items() if key != "categories"}, ensure_ascii=False))
    else:
        print(result, end="")
    return 0 if audit["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
