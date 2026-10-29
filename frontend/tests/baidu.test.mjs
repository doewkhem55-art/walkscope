import { test, mock } from 'node:test'
import assert from 'node:assert/strict'
import { metricScale, zoomMapView } from '../src/baidu.ts'

const key = 'browserTestKeyNotARealCredential'
let serial = 0
const freshLoader = async () => (await import(`../src/baidu.ts?case=${++serial}`)).loadBaiduMap
function fakeBrowser(t) {
  const scripts = []
  const previous = { window: globalThis.window, document: globalThis.document }
  globalThis.window = {}
  globalThis.document = {
    createElement: () => ({ remove() { this.removed = true } }),
    head: { appendChild(script) { scripts.push(script) } },
  }
  t.after(() => {
    for (const name of ['window', 'document']) {
      if (previous[name] === undefined) delete globalThis[name]
      else globalThis[name] = previous[name]
    }
    mock.timers.reset()
  })
  return scripts
}
function complete(script) {
  const name = new URL(script.src).searchParams.get('callback')
  window.BMap = { Map: class {}, Point: class {} }
  window[name]()
  return name
}

test('missing or malformed map AK makes no SDK request', async t => {
  const scripts = fakeBrowser(t)
  const load = await freshLoader()
  for (const bad of ['', ' ', 'not a browser key', '<script>']) await assert.rejects(load(bad), /浏览器端/)
  assert.equal(scripts.length, 0)
})

test('loads HTTPS JSAPI 4.0 once and rejects switching AK in one page', async t => {
  const scripts = fakeBrowser(t)
  const load = await freshLoader()
  const pending = load(key)
  assert.equal(load(key), pending)
  assert.equal(scripts.length, 1)
  const url = new URL(scripts[0].src)
  assert.equal(url.origin, 'https://api.map.baidu.com')
  assert.equal(url.pathname, '/api')
  assert.equal(url.searchParams.get('v'), '4.0')
  assert.equal(url.searchParams.has('type'), false)
  assert.equal(url.searchParams.get('ak'), key)
  complete(scripts[0])
  assert.equal(await pending, window.BMap)
  assert.equal(load(key), pending)
  await assert.rejects(load('anotherBrowserTestKeyNeverReal'), /刷新页面/)
  assert.equal(scripts.length, 1)
})

test('script errors are redacted and do not retry automatically', async t => {
  const scripts = fakeBrowser(t)
  const load = await freshLoader()
  const pending = load(key)
  scripts[0].onerror(new Error(`sensitive source ${key}`))
  await assert.rejects(pending, e => /加载失败/.test(e.message) && !e.message.includes(key))
  assert.equal(scripts[0].removed, true)
  assert.equal(load(key), pending)
  assert.equal(scripts.length, 1)
})

test('a callback without the expected SDK rejects instead of reporting success', async t => {
  const scripts = fakeBrowser(t)
  const load = await freshLoader()
  const pending = load(key)
  const callback = new URL(scripts[0].src).searchParams.get('callback')
  window[callback]()
  await assert.rejects(pending, /未能初始化/)
  assert.equal(scripts[0].removed, true)
})

test('timeout stays failed even when its callback arrives late', async t => {
  const scripts = fakeBrowser(t)
  mock.timers.enable({ apis: ['setTimeout'] })
  const load = await freshLoader()
  const pending = load(key)
  const rejection = assert.rejects(pending, /超时/)
  mock.timers.tick(15000)
  await rejection
  assert.doesNotThrow(() => complete(scripts[0]))
  await assert.rejects(load(key), /超时/)
  assert.equal(scripts.length, 1)
})


test('zoom controls use the SDK camera and the metric scale follows measured metres per pixel', () => {
  const calls = []
  const camera = { zoomIn() { calls.push('in') }, zoomOut() { calls.push('out') } }
  zoomMapView(camera, 1)
  zoomMapView(camera, -1)
  assert.deepEqual(calls, ['in', 'out'])
  assert.deepEqual(metricScale(2), { widthPx: 100, label: '200 米' })
  assert.deepEqual(metricScale(25), { widthPx: 80, label: '2 公里' })
  assert.deepEqual(metricScale(.15), { widthPx: 10 / .15, label: '10 米' })
  for (const invalid of [0, -1, NaN, Infinity]) assert.equal(metricScale(invalid), null)
})
