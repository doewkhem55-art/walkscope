import assert from 'node:assert/strict'
import test from 'node:test'
import { buildCheckupMapPois, filterCheckupMapPois, filterCoverageFeatures, filterGroupCoverageFeatures, parseCoverageLayers } from '../src/checkupMap.ts'

const point = { lng: 121.5, lat: 31.2, crs: 'BD09LL' }

test('keeps only in-circle POIs by default and deduplicates categories by poi_id', () => {
  const pois = buildCheckupMapPois([
    { poi_id: 'shared', name: '同一设施', facility_id: 'market-a', location: point, in_isochrone: true },
    { poi_id: 'shared', name: '同一设施', facility_id: 'school-a', location: point, in_isochrone: true },
    { poi_id: 'outside', name: '圈外候选', facility_id: 'market-a', location: { ...point, lng: 121.51 }, in_isochrone: false },
  ], [
    { id: 'market-a', name_zh: '菜市场', group_id: 'daily' },
    { id: 'school-a', name_zh: '小学', group_id: 'education' },
  ], [
    { id: 'daily', name_zh: '生活服务', color: '#b85c3d' },
    { id: 'education', name_zh: '教育', color: '#52728f' },
  ])

  assert.equal(pois.length, 2)
  assert.equal(pois[0].id, 'shared')
  assert.deepEqual(pois[0].memberships.map(item => [item.facilityId, item.groupId]), [
    ['market-a', 'daily'], ['school-a', 'education'],
  ])
  assert.deepEqual(filterCheckupMapPois(pois).map(item => item.id), ['shared'])
  assert.deepEqual(filterCheckupMapPois(pois, 'education').map(item => item.id), ['shared'])
  assert.deepEqual(filterCheckupMapPois(pois, 'daily', true).map(item => item.id), ['shared', 'outside'])
})

test('parses closed Polygon and MultiPolygon rings with holes; keeps unknown and blind layers separate', () => {
  const outer = [[121, 31], [122, 31], [122, 32], [121, 32], [121, 31]]
  const hole = [[121.2, 31.2], [121.8, 31.2], [121.8, 31.8], [121.2, 31.8], [121.2, 31.2]]
  const blind = {
    type: 'FeatureCollection',
    features: [
      { type: 'Feature', id: 'blind-1', geometry: { type: 'Polygon', coordinates: [outer, hole] }, properties: { facility_type: 'market-a' } },
      { type: 'Feature', id: 'open-ring', geometry: { type: 'Polygon', coordinates: [[outer[0], outer[1], outer[2], outer[3]]] }, properties: { facility_type: 'market-a' } },
    ],
  }
  const unknown = {
    type: 'FeatureCollection',
    features: [{ type: 'Feature', id: 'unknown-1', geometry: { type: 'MultiPolygon', coordinates: [[outer, hole], [outer]] }, properties: { facility_type: 'school-a' } }],
  }
  const layers = parseCoverageLayers({ blindspot: { blind_spots: blind, unknown_areas: unknown } })

  assert.equal(layers.blindSpots.length, 1)
  assert.equal(layers.blindSpots[0].kind, 'blind')
  assert.equal(layers.blindSpots[0].geometry[0].length, 2)
  assert.deepEqual(layers.blindSpots[0].geometry[0][0][0], layers.blindSpots[0].geometry[0][0].at(-1))
  assert.equal(layers.unknownAreas[0].kind, 'unknown')
  assert.equal(layers.unknownAreas[0].geometry.length, 2)
  assert.equal(layers.unknownAreas[0].geometry[0].length, 2)
  assert.deepEqual(filterCoverageFeatures(layers.blindSpots, 'school-a'), [])
  assert.deepEqual(filterCoverageFeatures(layers.unknownAreas, 'school-a').map(item => item.id), ['unknown-1'])
})

test('group coverage retains each child geometry including holes and excludes other groups', () => {
  const shape = { type: 'Polygon', coordinates: [[[121,31],[122,31],[122,32],[121,32],[121,31]],
    [[121.2,31.2],[121.8,31.2],[121.8,31.8],[121.2,31.8],[121.2,31.2]]] }
  const collection = facility_type => ({ type: 'Feature', properties: { facility_type }, geometry: shape })
  const layers = parseCoverageLayers({ blindspot: {
    blind_spots: { type: 'FeatureCollection', features: ['school', 'nursery', 'clinic'].map(collection) },
    unknown_areas: { type: 'FeatureCollection', features: [collection('school')] },
  } })
  const categories = [{ id: 'school', group_id: 'education' }, { id: 'nursery', group_id: 'education' }, { id: 'clinic', group_id: 'health' }]
  const selected = filterGroupCoverageFeatures(layers.blindSpots, categories, 'education')
  assert.deepEqual(selected.map(feature => feature.facilityId), ['school', 'nursery'])
  assert.equal(selected[0].geometry, layers.blindSpots[0].geometry, 'original rings are preserved, including holes')
  assert.equal(selected[0].geometry[0].length, 2)
  assert.deepEqual(filterGroupCoverageFeatures(layers.blindSpots, categories, ''), [])
  assert.equal(filterGroupCoverageFeatures(layers.unknownAreas, categories, 'education')[0].kind, 'unknown')
})
