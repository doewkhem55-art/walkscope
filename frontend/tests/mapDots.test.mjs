/**
 * Rule-dot-matrix tests for 阶段4a.
 *
 * These are pure-geography checks: which points belong to which walk-time tier, the
 * uniform circle size, opacity and geographic spacing with three blue time-band shades,
 * and the "no tiers means single layer, never fabricate
 * 5/10-minute rings from the 15-minute polygon" guard.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_DOTS,
  SINGLE_LAYER_DOT,
  TIER_DEFINITIONS,
  buildDotPlan,
  dotStyleFor,
  dotRadiusForSpacing,
  coverageAtPoint,
  parseIsochroneTiers,
  pointInGeometry,
  pointInRing,
  tierAtPoint,
} from '../src/mapDots.ts'

const ring = (minLng, minLat, size) => [
  [minLng, minLat], [minLng + size, minLat], [minLng + size, minLat + size], [minLng, minLat + size], [minLng, minLat],
]
const polygon = (...rings) => ({ type: 'Polygon', coordinates: rings })
/** parsePolygonGeometry returns {lng,lat} points; direct geometry tests need the same shape. */
const ringPoints = ring => ring.map(([lng, lat]) => ({ lng, lat }))
const RINGS = {
  outer: ring(121.49, 31.27, 0.02),
  middle: ring(121.495, 31.275, 0.01),
  inner: ring(121.499, 31.279, 0.002),
}
const singleReport = { isochrone: { geometry: polygon(RINGS.outer) } }
const tierReport = {
  isochrone: { geometry: polygon(RINGS.outer) },
  tiers: [
    { max_seconds: 900, geometry: polygon(RINGS.outer) },
    { max_seconds: 300, geometry: polygon(RINGS.inner) },
    { max_seconds: 600, geometry: polygon(RINGS.middle) },
  ],
}

test('three valid tiers share one size and opacity, ordered inner-to-outer by blue time-band shade', () => {
  const tiers = parseIsochroneTiers(tierReport)
  assert.deepEqual(tiers.map(tier => tier.id), ['5', '10', '15'])
  assert.deepEqual(tiers.map(tier => tier.maxSeconds), [300, 600, 900])
  assert.deepEqual(tiers.map(tier => tier.radiusPx), [2.2, 2.2, 2.2])
  assert.deepEqual(tiers.map(tier => tier.opacity), [.36, .36, .36])
  assert.deepEqual(tiers.map(tier => tier.color), ['#b8e5ff', '#609fcc', '#2e5f8a'])
  assert.deepEqual(tiers.map(tier => tier.label), ['0–5 分钟', '5–10 分钟', '10–15 分钟'])
  // The visual parameter table is the single source of the radii.
  assert.deepEqual(TIER_DEFINITIONS.map(tier => tier.radiusPx), [2.2, 2.2, 2.2])
})

test('the existing 15-minute polygon alone is never turned into three tiers', () => {
  assert.deepEqual(parseIsochroneTiers(singleReport), [], 'a single isochrone must not be extrapolated')
  const plan = buildDotPlan(singleReport)
  assert.equal(plan.mode, 'single')
  assert.deepEqual(plan.tiers, [])
  assert.ok(plan.points.length > 0)
  assert.ok(plan.points.every(point => point.tier === 'single'))
  assert.equal(plan.points.some(point => ['5', '10', '15'].includes(point.tier)), false, 'no fabricated tier dots')
  assert.equal(dotStyleFor(plan.points[0]).r, SINGLE_LAYER_DOT.radiusPx)
  assert.equal(dotStyleFor(plan.points[0]).r, 2.2)
})

test('two tiers are not enough: the plan stays a labelled single layer', () => {
  const partial = {
    isochrone: { geometry: polygon(RINGS.outer) },
    tiers: [
      { max_seconds: 300, geometry: polygon(RINGS.inner) },
      { max_seconds: 600, geometry: polygon(RINGS.middle) },
    ],
  }
  assert.deepEqual(parseIsochroneTiers(partial), [])
  assert.equal(buildDotPlan(partial).mode, 'single')
})

test('tier shapes are accepted through the isochrone.tiers alias and minute aliases', () => {
  const aliased = {
    isochrone: {
      geometry: polygon(RINGS.outer),
      tiers: [
        { minutes: 5, polygon: polygon(RINGS.inner) },
        { max_minutes: 10, boundary: polygon(RINGS.middle) },
        { max_duration_s: 900, geometry: polygon(RINGS.outer) },
      ],
    },
  }
  assert.deepEqual(parseIsochroneTiers(aliased).map(tier => tier.id), ['5', '10', '15'])

  // Provisional stage-3 shape where only the tier label is present.
  const labelled = {
    isochrone: { geometry: polygon(RINGS.outer) },
    tiers: [
      { tier_id: '5', geometry: polygon(RINGS.inner) },
      { level: 10, geometry: polygon(RINGS.middle) },
      { tier: '15', geometry: polygon(RINGS.outer) },
    ],
  }
  assert.deepEqual(parseIsochroneTiers(labelled).map(tier => tier.id), ['5', '10', '15'])
})

test('the in-progress stage-2A tier payload (tier_id/to_s/band_geojson) plugs in directly', () => {
  const tiers = [
    { tier_id: '0-300', from_s: 0, to_s: 300, cumulative_geojson: polygon(RINGS.outer), band_geojson: polygon(RINGS.inner) },
    { tier_id: '300-600', from_s: 300, to_s: 600, cumulative_geojson: polygon(RINGS.middle), band_geojson: polygon(RINGS.middle) },
    { tier_id: '600-900', from_s: 600, to_s: 900, cumulative_geojson: polygon(RINGS.outer), band_geojson: polygon(RINGS.outer) },
  ]
  const stageThree = { isochrone: { geometry: polygon(RINGS.outer), tiers } }
  const parsed = parseIsochroneTiers(stageThree)
  assert.deepEqual(parsed.map(tier => tier.id), ['5', '10', '15'])
  // The non-overlapping band geometry wins over the cumulative ring: an outer-ring point
  // must not be classified as the inner 5-minute tier.
  assert.equal(tierAtPoint(parsed, { lng: 121.5, lat: 31.28 }), '5')
  assert.equal(tierAtPoint(parsed, { lng: 121.492, lat: 31.287 }), '15')
  assert.equal(buildDotPlan(stageThree).mode, 'tiers')

  // A stored TieredIsochrone payload object under report.tiers is also understood.
  const payload = { isochrone: { geometry: polygon(RINGS.outer) }, tiers: { algorithm_version: 'grid-tiers-v1', thresholds_s: [300, 600, 900], tiers } }
  assert.deepEqual(parseIsochroneTiers(payload).map(tier => tier.id), ['5', '10', '15'])
})

test('the real stage-3 tiers-v1 object (report.isochrone.tiers) is understood and bands win', () => {
  // Compact fixture mirroring 执行资料/v3_2026-10-18/回放报告/tiers-demo-zhenpinglu_report.json
  // (schema_version tiers-v1, bands + cumulative in one object).
  const tiersV1 = {
    schema_version: 'tiers-v1',
    thresholds_s: [300, 600, 900],
    bands: [
      { tier_id: '0-300', from_s: 0, to_s: 300, label_zh: '5分钟', geometry: polygon(RINGS.inner) },
      { tier_id: '300-600', from_s: 300, to_s: 600, label_zh: '10分钟', geometry: polygon(RINGS.middle) },
      { tier_id: '600-900', from_s: 600, to_s: 900, label_zh: '15分钟', geometry: polygon(RINGS.outer) },
    ],
    cumulative: [
      { threshold_s: 300, label_zh: '5分钟', geometry: polygon(RINGS.outer), unknown_ratio: 0.2812 },
      { threshold_s: 600, label_zh: '10分钟', geometry: polygon(RINGS.outer), unknown_ratio: 0.2812 },
      { threshold_s: 900, label_zh: '15分钟', geometry: polygon(RINGS.outer), unknown_ratio: 0.2812 },
    ],
    time_source: 'route-matrix',
    sample_count: 317,
    unknown_ratio: 0.2812,
    unknown_mask: { type: 'FeatureCollection', features: [] },
    algorithm_version: 'grid-tiers-v1',
  }
  const report = { isochrone: { geometry: polygon(RINGS.outer), tiers: tiersV1 } }

  const parsed = parseIsochroneTiers(report)
  assert.deepEqual(parsed.map(tier => tier.id), ['5', '10', '15'])
  // Bands are non-overlapping and must beat the cumulative rings: an outer-ring point is
  // the 15-minute band, never the 5-minute band/cumulative.
  assert.equal(tierAtPoint(parsed, { lng: 121.5, lat: 31.28 }), '5')
  assert.equal(tierAtPoint(parsed, { lng: 121.497, lat: 31.283 }), '10')
  assert.equal(tierAtPoint(parsed, { lng: 121.492, lat: 31.287 }), '15')

  const plan = buildDotPlan(report)
  assert.equal(plan.mode, 'tiers')
  assert.deepEqual(plan.tiers.map(tier => tier.radiusPx), [2.2, 2.2, 2.2])
  assert.deepEqual([...new Set(plan.points.map(point => point.tier))].sort(), ['10', '15', '5'])
  for (const point of plan.points) assert.equal(tierAtPoint(plan.tiers, point), point.tier)
})

test('tiers-v1 cumulative geometry is accepted when bands are absent', () => {
  const cumulativeOnly = {
    isochrone: {
      geometry: polygon(RINGS.outer),
      tiers: {
        schema_version: 'tiers-v1',
        thresholds_s: [300, 600, 900],
        cumulative: [
          { threshold_s: 300, label_zh: '5分钟', geometry: polygon(RINGS.inner) },
          { threshold_s: 600, label_zh: '10分钟', geometry: polygon(RINGS.middle) },
          { threshold_s: 900, label_zh: '15分钟', geometry: polygon(RINGS.outer) },
        ],
      },
    },
  }
  const parsed = parseIsochroneTiers(cumulativeOnly)
  assert.deepEqual(parsed.map(tier => tier.id), ['5', '10', '15'])
  assert.equal(tierAtPoint(parsed, { lng: 121.5, lat: 31.28 }), '5')
  assert.equal(buildDotPlan(cumulativeOnly).mode, 'tiers')
})

test('a broken or incomplete tier geometry is rejected instead of guessed', () => {
  const openRing = polygon([[121.49, 31.27], [121.51, 31.27], [121.51, 31.29]]) // not closed
  const broken = {
    isochrone: { geometry: polygon(RINGS.outer) },
    tiers: [
      { max_seconds: 300, geometry: polygon(RINGS.inner) },
      { max_seconds: 600, geometry: polygon(RINGS.middle) },
      { max_seconds: 900, geometry: openRing },
    ],
  }
  assert.deepEqual(parseIsochroneTiers(broken), [])
})

test('points are classified once, using the shortest containing tier', () => {
  const tiers = parseIsochroneTiers(tierReport)
  assert.equal(tierAtPoint(tiers, { lng: 121.5, lat: 31.28 }), '5', 'inner ring')
  assert.equal(tierAtPoint(tiers, { lng: 121.497, lat: 31.283 }), '10', 'middle ring only')
  assert.equal(tierAtPoint(tiers, { lng: 121.492, lat: 31.287 }), '15', 'outer ring only')
  assert.equal(tierAtPoint(tiers, { lng: 121.4, lat: 31.2 }), null, 'outside every tier')

  const plan = buildDotPlan(tierReport)
  assert.equal(plan.mode, 'tiers')
  assert.equal(plan.tiers.length, 3)
  assert.ok(plan.points.length > 0)
  assert.ok(plan.points.length <= MAX_DOTS, `dot budget respected, got ${plan.points.length}`)
  for (const point of plan.points) {
    assert.equal(tierAtPoint(plan.tiers, point), point.tier, 'each point is drawn once with its shortest tier')
    assert.ok(['5', '10', '15'].includes(point.tier))
  }
  const ids = new Set(plan.points.map(point => point.tier))
  assert.equal(ids.has('5') && ids.has('10') && ids.has('15'), true, 'all three tiers are represented')
})

test('holes and disjoint components are honoured so dots are not painted over unknown holes', () => {
  const withHole = [ringPoints(RINGS.outer), ringPoints(RINGS.middle)]
  assert.equal(pointInGeometry([withHole], { lng: 121.5, lat: 31.28 }), false, 'inside the hole')
  assert.equal(pointInGeometry([withHole], { lng: 121.492, lat: 31.287 }), true, 'inside the outer ring only')

  const multipolygon = [[ringPoints(RINGS.inner)], [ringPoints(ring(121.6, 31.4, 0.002))]]
  assert.equal(pointInGeometry(multipolygon, { lng: 121.5, lat: 31.28 }), true)
  assert.equal(pointInGeometry(multipolygon, { lng: 121.601, lat: 31.401 }), true)
  assert.equal(pointInGeometry(multipolygon, { lng: 121.55, lat: 31.35 }), false)
})

test('ring containment is even-odd and boundary-agnostic', () => {
  assert.equal(pointInRing(ringPoints(RINGS.outer), { lng: 121.5, lat: 31.28 }), true)
  assert.equal(pointInRing(ringPoints(RINGS.outer), { lng: 121.6, lat: 31.4 }), false)
  assert.equal(buildDotPlan({}).mode, 'none', 'no geometry at all draws nothing')
})

test('dot styles share circle size and opacity, with only the time-band color changing', () => {
  const tiers = parseIsochroneTiers(tierReport)
  const plan = buildDotPlan(tierReport)
  for (const point of plan.points) {
    const style = dotStyleFor(point)
    const expected = tiers.find(tier => tier.id === point.tier)
    assert.equal(style.r, expected.radiusPx)
    assert.equal(style.fill, expected.color)
  }
  assert.equal(dotStyleFor({ lng: 0, lat: 0, tier: 'single' }).r, SINGLE_LAYER_DOT.radiusPx)
})


test('the dense matrix shares a globally anchored staggered hexagonal lattice across bands, deterministic and bounded', () => {
  const plan = buildDotPlan(tierReport)
  assert.deepEqual(buildDotPlan(tierReport), plan, 'camera-independent geometry gives stable anchored dots')
  assert.ok(plan.points.length > 1800, 'the shared lattice is denser than the previous bounding-box grid')
  assert.ok(plan.points.length <= MAX_DOTS)
  assert.equal(new Set(plan.points.map(point => `${point.lng}:${point.lat}`)).size, plan.points.length)
  const dLat = plan.spacingM * Math.sqrt(3) / 2 / 111320
  const dLng = plan.spacingM / (111320 * Math.cos(31.28 * Math.PI / 180))
  const rows = new Map()
  for (const point of plan.points) {
    const row = Math.round(point.lat/dLat)
    if (!rows.has(row)) rows.set(row, [])
    rows.get(row).push(point.lng)
  }
  const firstRows = [...rows.keys()].sort((a,b) => a-b).slice(0, 2)
  const rowOffset = Math.abs((rows.get(firstRows[1])[0]-rows.get(firstRows[0])[0])/dLng)
  assert.ok(Math.abs(rowOffset-.5) < 1e-6, 'adjacent rows are offset by half a horizontal step')
  for (const point of plan.points) {
    const row = point.lat/dLat
    const globalRow = Math.round(row)
    const halfStep = ((globalRow % 2 + 2) % 2) / 2
    const col = point.lng/dLng-halfStep
    assert.ok(Math.abs(row-globalRow) < 1e-6, 'every band shares global latitude rows')
    assert.ok(Math.abs(col-Math.round(col)) < 1e-6, 'row parity fixes the global half-step offset')
    assert.equal(dotStyleFor(point).r, 2.2)
    assert.equal(dotStyleFor(point).opacity, .36)
  }
  const hollow = { isochrone: { geometry: polygon(RINGS.outer, RINGS.middle) } }
  const hollowPlan = buildDotPlan(hollow)
  assert.ok(hollowPlan.points.length > 0 && hollowPlan.points.length <= MAX_DOTS)
  assert.ok(hollowPlan.points.every(point => !pointInGeometry([[ringPoints(RINGS.middle)]], point)), 'unmeasured holes stay empty')
})


test('projected grid radius stays proportional at normal zoom, shared and safely bounded', () => {
  assert.equal(dotRadiusForSpacing(10), 4.8)
  assert.equal(dotRadiusForSpacing(20), 9.6)
  assert.equal(dotRadiusForSpacing(.1), .8)
  assert.equal(dotRadiusForSpacing(100), 48)
  assert.equal(dotRadiusForSpacing(1000), 128)
  assert.equal(dotRadiusForSpacing(NaN), 2.2)
})


test('selected-category coverage classifies real dot centers, with unknown areas never treated as blind', () => {
  const blind = [[ringPoints(RINGS.outer), ringPoints(RINGS.inner)]]
  const unknown = [[ringPoints(RINGS.middle)]]
  assert.equal(coverageAtPoint({ lng: 121.492, lat: 31.287 }, [blind], [unknown]), 'blind')
  assert.equal(coverageAtPoint({ lng: 121.497, lat: 31.283 }, [blind], [unknown]), 'unknown')
  assert.equal(coverageAtPoint({ lng: 121.5, lat: 31.28 }, [blind], []), 'covered', 'a blind geometry hole is not blind')
  assert.equal(coverageAtPoint({ lng: 121.4, lat: 31.2 }, [blind], [unknown]), 'covered')
})
