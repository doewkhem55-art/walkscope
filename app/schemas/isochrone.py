"""Pydantic v2 contracts for approximate walking isochrones."""

import math
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import Field, RootModel, model_validator

from app.schemas.common import (
    CenterPoint,
    ContractModel,
    ErrorDetail,
    Meta,
    SchemaVersion,
    WarningItem,
)


class IsochroneConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IsochroneErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ROUTING_FAILED = "ROUTING_FAILED"
    INSUFFICIENT_ROUTING_DATA = "INSUFFICIENT_ROUTING_DATA"
    INVALID_GEOMETRY = "INVALID_GEOMETRY"


class IsochroneRequest(ContractModel):
    schema_version: SchemaVersion
    center: CenterPoint
    time_limit_s: int = Field(gt=0)


def _validate_ring(ring: list[list[float]], *, allow_holes: bool = False) -> None:
    if len(ring) < 4 or ring[0] != ring[-1]:
        raise ValueError("Polygon 环必须至少三个顶点并闭合。")
    if len({tuple(point) for point in ring[:-1]}) < 3:
        raise ValueError("Polygon 环至少需要三个不同顶点。")
    for point in ring:
        if (
            len(point) != 2
            or not all(math.isfinite(value) for value in point)
            or not -180 <= point[0] <= 180
            or not -90 <= point[1] <= 90
        ):
            raise ValueError("GeoJSON 坐标必须是合法的 [lng, lat]。")


class GeoJSONPolygon(ContractModel):
    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]

    @model_validator(mode="after")
    def valid_single_outer_ring(self) -> "GeoJSONPolygon":
        if len(self.coordinates) != 1:
            raise ValueError("Isochrone v1 仅支持一个无孔外环；带孔几何请使用 MultiPolygon。")
        _validate_ring(self.coordinates[0])
        return self


class GeoJSONMultiPolygon(ContractModel):
    """GeoJSON MultiPolygon with optional interior rings per polygon."""

    type: Literal["MultiPolygon"]
    coordinates: list[list[list[list[float]]]]

    @model_validator(mode="after")
    def valid_multipolygon(self) -> "GeoJSONMultiPolygon":
        if not self.coordinates:
            raise ValueError("MultiPolygon 至少需要一个面。")
        for polygon in self.coordinates:
            if not polygon:
                raise ValueError("MultiPolygon 的每个面至少需要一个外环。")
            for ring in polygon:
                _validate_ring(ring)
        return self


IsochroneGeometry = Annotated[
    Union[GeoJSONPolygon, GeoJSONMultiPolygon], Field(discriminator="type")
]


class TierCumulative(ContractModel):
    """Cumulative ``duration <= threshold_s`` range for one display tier."""

    threshold_s: int = Field(gt=0)
    label_zh: str = Field(min_length=1)
    geometry: IsochroneGeometry
    unknown_ratio: float | None = Field(default=None, ge=0, le=1)


class TierBand(ContractModel):
    """Non-overlapping display band ``from_s < duration <= to_s``."""

    tier_id: str = Field(min_length=1)
    from_s: int = Field(ge=0)
    to_s: int = Field(gt=0)
    label_zh: str = Field(min_length=1)
    geometry: IsochroneGeometry

    @model_validator(mode="after")
    def ordered_range(self) -> "TierBand":
        if self.to_s <= self.from_s:
            raise ValueError("环带时间范围必须递增。")
        return self


class IsochroneUnknownMask(ContractModel):
    """Unmeasured grid cells: ``row:col`` ids plus an optional map overlay."""

    mask_cells: list[str] = Field(default_factory=list, max_length=100_000)
    mask_geometry: IsochroneGeometry | None = None

    @model_validator(mode="after")
    def cells_are_row_col(self) -> "IsochroneUnknownMask":
        for value in self.mask_cells:
            row, separator, col = value.partition(":")
            if not separator or not row.isdigit() or not col.isdigit():
                raise ValueError("未知掩膜单元格式必须是 'row:col'。")
        return self


class IsochroneTiers(ContractModel):
    """5/10/15-minute cumulative ranges + non-overlapping bands for one field.

    Unknown grid cells are never interpolated into a tier; the mask and ratio
    are reported alongside so a client can render "unknown" instead of
    "outside 15 minutes".
    """

    schema_version: Literal["tiers-v1"] = "tiers-v1"
    thresholds_s: list[int] = Field(min_length=3, max_length=3)
    cumulative: list[TierCumulative] = Field(min_length=3, max_length=3)
    bands: list[TierBand] = Field(min_length=3, max_length=3)
    time_source: str = Field(min_length=1)
    sample_count: int = Field(ge=0)
    unknown_ratio: float = Field(ge=0, le=1)
    unknown_mask: IsochroneUnknownMask | None = None
    algorithm_version: str = Field(default="grid-tiers-v1", min_length=1)

    @model_validator(mode="after")
    def three_nested_tiers(self) -> "IsochroneTiers":
        thresholds = self.thresholds_s
        if any(value <= 0 for value in thresholds):
            raise ValueError("时间阈值必须为正整数秒。")
        if any(later <= earlier for earlier, later in zip(thresholds, thresholds[1:])):
            raise ValueError("时间阈值必须严格递增。")
        if [item.threshold_s for item in self.cumulative] != thresholds:
            raise ValueError("累计范围阈值与 thresholds_s 不一致。")
        expected = [
            (0 if index == 0 else thresholds[index - 1], thresholds[index])
            for index in range(len(thresholds))
        ]
        if [(band.from_s, band.to_s) for band in self.bands] != expected:
            raise ValueError("环带范围必须与阈值序列首尾相接。")
        expected_ids = [f"{start}-{end}" for start, end in expected]
        ids = [band.tier_id for band in self.bands]
        if ids != expected_ids:
            raise ValueError(f"环带 tier_id 必须依次为 {expected_ids}。")
        return self


class IsochroneTimeField(ContractModel):
    """Saved local-metre time field; ``null`` in ``times_s`` means unknown.

    The field is enough to replay tiers offline: with the report centre it can
    rebuild the same grid and contour without another route request.
    """

    crs: Literal["local-m"] = "local-m"
    origin_x_m: float
    origin_y_m: float
    cell_m: float = Field(gt=0, allow_inf_nan=False)
    rows: int = Field(gt=1)
    cols: int = Field(gt=1)
    time_limit_s: int = Field(gt=0)
    times_s: list[list[float | None]]
    source: str = Field(min_length=1)
    collected_at: str | None = None

    @model_validator(mode="after")
    def grid_shape_and_values(self) -> "IsochroneTimeField":
        if len(self.times_s) != self.rows:
            raise ValueError("times_s 行数与 rows 不一致。")
        for row in self.times_s:
            if len(row) != self.cols:
                raise ValueError("times_s 列数与 cols 不一致。")
            for value in row:
                if value is None:
                    continue
                if not math.isfinite(value) or value < 0:
                    raise ValueError("times_s 必须是有限非负数，未知只能用 null。")
        return self


class BoundaryPoint(ContractModel):
    boundary_id: str = Field(min_length=1)
    bearing_deg: float = Field(ge=0, lt=360, allow_inf_nan=False)
    location: CenterPoint
    radial_distance_m: float = Field(ge=0, allow_inf_nan=False)


class IsochroneQuality(ContractModel):
    requested_directions: int = Field(default=0, ge=0)
    valid_boundary_directions: int = Field(default=0, ge=0)
    routing_sample_count: int = Field(ge=1)
    routing_success_count: int = Field(ge=0)
    sample_success_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    confidence: IsochroneConfidence = Field(strict=False)
    unknown_ratio: float | None = Field(default=None, ge=0, le=1)
    grid_cell_m: float | None = Field(default=None, gt=0)
    algorithm_version: str = "radial-v1"

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "IsochroneQuality":
        if self.requested_directions and self.valid_boundary_directions > self.requested_directions:
            raise ValueError("有效边界方向不能超过请求方向。")
        if self.routing_success_count > self.routing_sample_count:
            raise ValueError("成功样本数不能超过总样本数。")
        expected = self.routing_success_count / self.routing_sample_count
        if abs(self.sample_success_rate - expected) > 0.0001:
            raise ValueError("sample_success_rate 与样本数量不一致。")
        return self


class IsochroneData(ContractModel):
    center: CenterPoint
    time_limit_s: int = Field(gt=0)
    geometry: IsochroneGeometry
    algorithm_version: str = "radial-v1"
    boundary_points: list[BoundaryPoint] = Field(default_factory=list)
    quality: IsochroneQuality
    # Additive v1.2 fields. Older reports omit them and still load; a report
    # without ``time_field`` can only show the existing single 15-minute layer.
    tiers: IsochroneTiers | None = None
    time_field: IsochroneTimeField | None = None

    @model_validator(mode="after")
    def boundary_and_geometry_are_consistent(self) -> "IsochroneData":
        if not self.algorithm_version.startswith("radial"):
            # Grid contours keep holes/multiple polygons and do not have a
            # bearing-ordered outer ring; the geometry model validates itself.
            if self.boundary_points:
                raise ValueError("非径向算法不应输出按方位排序的 boundary_points。")
            return self
        if self.geometry.type != "Polygon":
            raise ValueError("径向算法只输出单个 Polygon。")
        points = self.boundary_points
        if len(points) < 3:
            raise ValueError("径向算法至少需要三个边界方向。")
        if len(points) != self.quality.valid_boundary_directions:
            raise ValueError("boundary_points 与有效方向数不一致。")
        if len({point.boundary_id for point in points}) != len(points):
            raise ValueError("boundary_id 必须唯一。")
        bearings = [point.bearing_deg for point in points]
        if bearings != sorted(bearings) or len(set(bearings)) != len(bearings):
            raise ValueError("boundary_points 必须按唯一 bearing 排序。")
        expected_ring = [[point.location.lng, point.location.lat] for point in points]
        expected_ring.append(expected_ring[0])
        if self.geometry.coordinates[0] != expected_ring:
            raise ValueError("Polygon 外环必须由 boundary_points 按顺序闭合生成。")
        return self

    @model_validator(mode="after")
    def tiers_match_time_field(self) -> "IsochroneData":
        """Cross-check the optional tiers/time_field against the report itself."""
        if self.tiers is not None:
            if self.tiers.cumulative[-1].threshold_s != self.time_limit_s:
                raise ValueError("三档最大累计阈值必须等于 time_limit_s。")
        if self.time_field is not None:
            if self.time_field.time_limit_s != self.time_limit_s:
                raise ValueError("time_field.time_limit_s 与报告不一致。")
            if (
                self.quality.grid_cell_m is not None
                and abs(self.time_field.cell_m - self.quality.grid_cell_m) > 1e-9
            ):
                raise ValueError("time_field.cell_m 与 quality.grid_cell_m 不一致。")
        if self.tiers is not None and self.time_field is not None:
            measured = sum(
                1 for row in self.time_field.times_s for value in row if value is not None
            )
            if measured != self.tiers.sample_count:
                raise ValueError("time_field 测时点数必须与 tiers.sample_count 一致。")
            cells = self.time_field.rows * self.time_field.cols
            unknown = cells - measured
            expected_ratio = unknown / cells
            if abs(expected_ratio - self.tiers.unknown_ratio) > 1e-6:
                raise ValueError("tiers.unknown_ratio 与 time_field 未知格数不一致。")
            if (
                self.tiers.unknown_mask is not None
                and len(self.tiers.unknown_mask.mask_cells) != unknown
            ):
                raise ValueError("未知掩膜单元数必须与 time_field 未知格数一致。")
        return self


class IsochroneErrorDetail(ErrorDetail):
    code: IsochroneErrorCode = Field(strict=False)


class IsochroneSuccess(ContractModel):
    ok: Literal[True]
    data: IsochroneData
    warnings: list[WarningItem]
    meta: Meta


class IsochroneFailure(ContractModel):
    ok: Literal[False]
    error: IsochroneErrorDetail
    warnings: list[WarningItem]
    meta: Meta


IsochroneEnvelope = Annotated[
    Union[IsochroneSuccess, IsochroneFailure],
    Field(discriminator="ok"),
]


class IsochroneResult(RootModel[IsochroneEnvelope]):
    pass
