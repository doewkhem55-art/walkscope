/**
 * 阶段4a view-contract tests.
 *
 * The geometry/tier maths lives in mapDots.test.mjs and the dark-style fallback decision in
 * mapStyle.test.mjs. This file guards the page structure that cannot be unit-rendered
 * offline: dark basemap wiring, non-click-capturing dot layer, the "仅单层" label, the
 * compact toolbar and the narrow-screen bottom drawer.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from 'vue/compiler-sfc'

const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')
const mapSource = read('BaiduRoadMap.vue')
const mapTemplate = parse(mapSource).descriptor.template?.content ?? ''
const appSource = read('CheckupApp.vue')
const appTemplate = parse(appSource).descriptor.template?.content ?? ''
const globalCss = read('style.css')
const conversationSource = read('ReportConversation.vue')
const reportSource = read('CheckupReport.vue')

test('the default dark path is the verified CSS filter, not the silently-ignored SDK style', () => {
  assert.ok(mapSource.includes("from './mapStyle.ts'"), 'the dark strategy is applied through mapStyle.ts')
  assert.ok(mapSource.includes('resolveDarkMapMode(import.meta.env)'), 'the env switch is resolved explicitly')
  assert.ok(mapSource.includes('applyDarkMapStyle(map, darkMapMode)'), 'applied right after map creation')
  assert.ok(mapSource.includes("'is-dark-filter': mapStyleMode === 'filter'"), 'filter class is bound')
  assert.ok(mapSource.includes(':data-map-style="mapStyleMode"'), 'the actual applied path is observable for QA')
  assert.ok(mapSource.includes(':data-map-mode="darkMapMode"'), 'the requested mode is observable for QA')
  assert.match(mapSource, /\.road-map\.is-dark-filter \.road-map-canvas \{ filter:/, 'the filter turns the tile canvas dark')
  assert.ok(mapSource.includes("'底图：深色滤镜（默认可验证）'"), 'the filter path is labelled truthfully')
  assert.ok(mapSource.includes("'底图：自定义深色样式（已显式确认）'"), 'custom is only claimed when confirmed')
  assert.ok(mapSource.includes('data-map-style-note'))
  assert.ok(mapSource.includes(':data-map-filter="mapFilterCss"'), 'the exact filter value is observable in the DOM')
  assert.ok(mapSource.includes('DARK_MAP_FILTER_CSS'), 'the filter is defined once in mapStyle.ts')
})

test('the console-404 favicon is declared and shipped, and the env switch is documented', () => {
  const index = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  assert.match(index, /<link rel="icon" type="image\/svg\+xml" href="\/favicon\.svg"/,
    'an explicit icon stops the implicit /favicon.ico request')
  const favicon = readFileSync(new URL('../public/favicon.svg', import.meta.url), 'utf8')
  assert.match(favicon, /<svg/, 'the shipped favicon is a real file (no SPA HTML fallback)')
  const env = readFileSync(new URL('../.env.example', import.meta.url), 'utf8')
  assert.match(env, /VITE_DARK_MAP_MODE=filter/, 'the default mode is documented')
  assert.match(env, /VITE_DARK_MAP_CUSTOM_CONFIRMED=false/, 'custom style is confirmation-gated')
})

test('the dot matrix is one non-click-capturing SVG layer under the panels', () => {
  assert.ok(mapSource.includes("from './mapDots.ts'"), 'the rule grid comes from mapDots.ts')
  assert.ok(mapTemplate.includes('class="checkup-dots"'), 'the dots have their own layer')
  assert.ok(mapTemplate.includes('v-for="dot in visibleDots"'), 'dots are projected from the geographic plan')
  assert.ok(mapTemplate.includes(':r="dot.r"'), 'the per-tier radius is rendered')
  assert.ok(mapTemplate.includes(':fill-opacity="dot.opacity"'), 'the per-tier brightness is rendered')
  assert.ok(mapSource.includes('.checkup-dots { pointer-events: none; }'), 'the dot layer never captures clicks')
  assert.match(mapSource, /\.checkup-map-overlay \{[^}]*pointer-events: none;/, 'the overlay defaults to pass-through')
  assert.ok(mapTemplate.includes(':data-dot-mode="dotPlan.mode"'), 'tier/single mode is observable for QA')
})

test('single-layer reports are labelled 仅单层 and no fake tiers are rendered', () => {
  assert.ok(mapTemplate.includes('dotPlan.mode === \'tiers\''), 'tier rendering is gated on real tier data')
  assert.ok(mapTemplate.includes('仅单层'), 'the single-layer label is visible')
  assert.ok(mapTemplate.includes('服务未返回 5/10/15 分钟分档数据'), 'the reason is stated, not implied')
  assert.ok(mapTemplate.includes('15分钟步行等时圈（仅单层）'), 'the existing polygon keeps its own label')
  assert.ok(mapTemplate.includes('未生成内层'), 'no inner rings are synthesized')
  assert.ok(mapSource.includes('dotPlan.mode !== \'tiers\''), 'the single polygon is hidden when real tiers exist')
})

test('the dot legend never implies facilities or population', () => {
  assert.ok(mapTemplate.includes('不代表设施或人口数量'), 'the dot legend states what the dots are not')
  assert.ok(mapTemplate.includes('统一点距'), 'the shared spacing rule is stated')
  assert.ok(mapTemplate.includes('每个位置只绘制一次'), 'the one-dot-per-location rule is stated')
  assert.ok(mapTemplate.includes('显示设施点'), 'facilities stay a separate, toggleable layer')
})

test('workspace places search above the map and seven score cards at its base', () => {
  assert.match(appTemplate, /class="search-glass glass"/)
  assert.match(appTemplate, /class="horizontal-groups"/)
  assert.match(appTemplate, /class="score-dock"/)
  assert.match(appTemplate, /class="question-glass glass"/)
  assert.match(appTemplate, /aria-label="地址建议"/)
  assert.match(appTemplate, /v-if="allGroupsComplete/)
  assert.doesNotMatch(appTemplate, /v-for="facility in/)
})

test('panels stay above the map stacking context so BMap_mask cannot swallow clicks', () => {
  assert.match(globalCss, /\.map-panel \{[\s\S]*?z-index: 1;/, 'the map creates the lower stacking context')
  assert.match(globalCss, /\.panel \{[\s\S]*?z-index: 4;/, 'the panels sit above the map context')
  assert.match(globalCss, /\.workspace \{[\s\S]*?position: relative;/, 'the map and panels share one positioning context')
  assert.ok(mapSource.includes('z-index inside the map stacking context stays below the floating panels'),
    'the layering rule is documented where the overlay is defined')
})

test('report text stays blue-white and panel fill is seventy percent transparent', () => {
  assert.match(reportSource, /color: var\(--ink\)/)
  assert.match(reportSource, /color: #e7f7ff/)
  assert.match(reportSource, /color: #b8e7ff/)
  assert.match(globalCss, /--panel: rgba\(7, 21, 35, 0\.30\)/, 'only the panel fill is transparent')
  assert.match(globalCss, /--paper: #070c14/)
  assert.match(globalCss, /--ink: #e8eef7/)
  assert.match(conversationSource, /\.answer-card \{[^}]*background: rgba\(255, 255, 255, \.04\)/)
  assert.doesNotMatch(reportSource, /#f[cfab][0-9a-f]{4}|orange|goldenrod/i)
})

test('no third-party visualisation library or SDK wrapper was added', () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.deepEqual(Object.keys(pkg.dependencies), ['vue'], 'runtime dependencies stay at vue only')
  assert.doesNotMatch(JSON.stringify(pkg), /d3|echarts|leaflet|mapbox|deck\.gl|three|plotly|chart\.js/,
    'no visualisation dependency is introduced')
  for (const source of [mapSource, appSource]) {
    assert.doesNotMatch(source, /from ['"](d3|echarts|leaflet|mapbox-gl|three)/, 'imports stay local/Vue only')
  }
})
