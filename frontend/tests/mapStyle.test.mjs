/**
 * Dark basemap strategy tests for 阶段4b.
 *
 * Browser evidence showed JSAPI can silently ignore setMapStyleV2/setMapStyle. These tests
 * lock the resulting rule: the verified CSS filter is the default, and the custom SDK style
 * is only used when explicitly requested AND explicitly confirmed.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { DARK_MAP_FILTER_CSS, DARK_MAP_STYLE_JSON, applyDarkMapStyle, resolveDarkMapMode } from '../src/mapStyle.ts'

test('the verified CSS filter actually inverts/darkens and stays scoped to the tiles', () => {
  assert.match(DARK_MAP_FILTER_CSS, /invert\(1\)/)
  assert.match(DARK_MAP_FILTER_CSS, /hue-rotate\(180deg\)/)
  assert.match(DARK_MAP_FILTER_CSS, /brightness\(/)
  assert.match(DARK_MAP_FILTER_CSS, /contrast\(/)
  assert.doesNotMatch(DARK_MAP_FILTER_CSS, /url\(|drop-shadow|blur\(/, 'no filter that could smear labels')
})

test('the verified CSS filter is the default and never claims custom', () => {
  assert.equal(resolveDarkMapMode({}), 'filter')
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'filter' }), 'filter')
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'AUTO' }), 'filter', 'auto cannot prove a silent SDK no-op')
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'nonsense' }), 'filter')
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'custom' }), 'filter', 'custom alone is not enough')
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'custom', VITE_DARK_MAP_CUSTOM_CONFIRMED: 'false' }), 'filter')
})

test('confirmed custom mode is the only way to request the SDK style', () => {
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'custom', VITE_DARK_MAP_CUSTOM_CONFIRMED: 'true' }), 'custom')
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_MODE: 'custom', VITE_DARK_MAP_CUSTOM_CONFIRMED: '1' }), 'custom')
  // A stray confirmation flag without custom requested must not change the safe default.
  assert.equal(resolveDarkMapMode({ VITE_DARK_MAP_CUSTOM_CONFIRMED: 'true' }), 'filter')
})

test('filter mode never touches the SDK map style', () => {
  const calls = []
  const mode = applyDarkMapStyle({
    setMapStyleV2: () => calls.push('v2'),
    setMapStyle: () => calls.push('v1'),
  }, 'filter')
  assert.equal(mode, 'filter')
  assert.deepEqual(calls, [])
})

test('custom mode tries JSAPI 4.0 setMapStyleV2 with the dark style JSON', () => {
  const calls = []
  const mode = applyDarkMapStyle({ setMapStyleV2: options => calls.push(['v2', options]) }, 'custom')
  assert.equal(mode, 'custom')
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], 'v2')
  assert.equal(calls[0][1].styleJson, DARK_MAP_STYLE_JSON)
})

test('custom mode falls back to the older entry point, then to the filter', () => {
  const calls = []
  assert.equal(applyDarkMapStyle({ setMapStyle: options => calls.push(options) }, 'custom'), 'custom')
  assert.equal(calls.length, 1)

  assert.equal(applyDarkMapStyle({
    setMapStyleV2: () => { throw new Error('unsupported style version') },
    setMapStyle: () => {},
  }, 'custom'), 'custom', 'the older entry point is still tried')

  assert.equal(applyDarkMapStyle({}, 'custom'), 'filter')
  assert.equal(applyDarkMapStyle({
    setMapStyleV2: () => { throw new Error('rejected') },
    setMapStyle: () => { throw new Error('rejected') },
  }, 'custom'), 'filter')
})

test('the custom dark style keeps roads identifiable and labels visible', () => {
  const background = DARK_MAP_STYLE_JSON.find(rule => rule.featureType === 'background')
  assert.ok(background, 'the background must be styled')
  const colour = background.stylers.color
  assert.match(colour, /^#0[0-9a-f]{5}$/i, 'a dark blue-black background')
  assert.ok(Number.parseInt(colour.slice(1, 3), 16) < 0x30)

  const roads = DARK_MAP_STYLE_JSON.filter(rule => rule.featureType === 'road')
  assert.ok(roads.some(rule => rule.elementType === 'geometry'), 'road geometry must stay drawn')
  const roadLabels = roads.find(rule => rule.elementType === 'labels')
  assert.equal(roadLabels?.stylers?.visibility, 'on', 'road names must remain readable')
  assert.ok(roads.some(rule => rule.stylers.color && Number.parseInt(rule.stylers.color.slice(1, 3), 16) > 0x10))

  for (const feature of ['district', 'city']) {
    const labels = DARK_MAP_STYLE_JSON.find(rule => rule.featureType === feature && rule.elementType === 'labels')
    assert.ok(labels, `${feature} labels must be styled`)
    assert.notEqual(labels.stylers.visibility, 'off', `${feature} names must stay readable`)
  }
  // Report facilities are the only facility markers on the map.
  for (const feature of ['poi', 'poilabel']) {
    const hidden = DARK_MAP_STYLE_JSON.find(rule => rule.featureType === feature && rule.elementType === 'labels')
    assert.equal(hidden?.stylers?.visibility, 'off', `${feature} labels must not compete with report points`)
  }
})
