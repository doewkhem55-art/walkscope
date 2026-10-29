// Only the JSAPI 4.0 surface used by this page; no extra Vue map wrapper.
export interface BaiduPoint { lng: number; lat: number }
export interface BaiduMap {
  pointToPixel(point: BaiduPoint): { x: number; y: number }
  centerAndZoom(point: BaiduPoint, zoom: number): void
  setViewport(points: BaiduPoint[], options?: { margins?: number[]; enableAnimation?: boolean; callback?: () => void }): void
  zoomIn(point?: BaiduPoint): void
  zoomOut(point?: BaiduPoint): void
  disableScrollWheelZoom?(): void
  pixelToPoint?(pixel: { x: number; y: number }): BaiduPoint
  getDistance?(start: BaiduPoint, end: BaiduPoint): number
  checkResize(): void
  destroy(): void
  addEventListener(event: string, handler: () => void): void
  removeEventListener(event: string, handler: () => void): void
  // JSAPI 4.0 personalization (styleJson) and older 3.x fallback. Either may be absent;
  // see mapStyle.ts for the dark-style application and the CSS-filter fallback.
  setMapStyleV2?(options: { styleJson: ReadonlyArray<Record<string, unknown>> }): void
  setMapStyle?(options: { styleJson: ReadonlyArray<Record<string, unknown>> }): void
}
export interface BaiduSearchPoi {title:string;address:string;point:BaiduPoint}
export interface BaiduSearchResult {getCurrentNumPois():number;getPoi(index:number):BaiduSearchPoi}
export interface BaiduNamespace {
  LocalSearch?: new (location: string | BaiduPoint, options: {pageCapacity:number;onSearchComplete:(result:BaiduSearchResult)=>void}) => {search(query:string):void}

  Map: new (container: HTMLElement, options?: {
    center?: BaiduPoint; zoom?: number; heading?: number; tilt?: number
    enableRotate?: boolean; enableRotateGestures?: boolean
    enableTilt?: boolean; enableTiltGestures?: boolean
    enableWheelZoom?: boolean; enableAutoResize?: boolean; enableMapClick?: boolean
  }) => BaiduMap
  Point: new (lng: number, lat: number) => BaiduPoint
  Pixel?: new (x: number, y: number) => { x: number; y: number }
}
declare global { interface Window { BMap?: BaiduNamespace } }

let request: { ak: string; promise: Promise<BaiduNamespace> } | undefined

// Use the SDK camera; never resize the page, the map DOM or its overlay by CSS.
export function zoomMapView(map: Pick<BaiduMap, 'zoomIn' | 'zoomOut'>, direction: 1 | -1): void {
  if (direction > 0) map.zoomIn()
  else map.zoomOut()
}

export function metricScale(metersPerPixel: number, maxWidthPx = 100): { widthPx: number; label: string } | null {
  if (!Number.isFinite(metersPerPixel) || metersPerPixel <= 0 || !Number.isFinite(maxWidthPx) || maxWidthPx <= 0) return null
  const maximum = metersPerPixel * maxWidthPx
  const power = 10 ** Math.floor(Math.log10(maximum))
  const meters = [5, 2, 1].map(n => n * power).find(n => n <= maximum) ?? power / 2
  return { widthPx: meters / metersPerPixel, label: meters >= 1000 ? `${meters / 1000} 公里` : `${meters} 米` }
}

export function loadBaiduMap(ak: string): Promise<BaiduNamespace> {
  const key = ak.trim()
  if (!/^[a-zA-Z0-9]{16,128}$/.test(key)) {
    return Promise.reject(new Error('请在 .env.local 中配置有效的浏览器端 VITE_BAIDU_MAP_AK，再重启前端。'))
  }
  if (request) {
    return request.ak === key ? request.promise
      : Promise.reject(new Error('地图配置已改变，请刷新页面后重新加载。'))
  }
  const promise = new Promise<BaiduNamespace>((resolve, reject) => {
    if (typeof window === 'undefined' || typeof document === 'undefined') {
      reject(new Error('百度底图只能在浏览器页面中加载。'))
      return
    }
    const callbackName = `__lifeCircleBaidu_${Date.now()}_${Math.random().toString(36).slice(2)}`
    const callbacks = window as unknown as Record<string, unknown>
    const script = document.createElement('script')
    let settled = false
    const cleanup = () => {
      clearTimeout(timer)
      script.onerror = null
      // A timed-out remote script can still arrive. Keep one harmless callback
      // for this page lifetime; never let it revive a failed map request.
      callbacks[callbackName] = () => {}
    }
    const fail = (message: string) => {
      if (settled) return
      settled = true
      cleanup()
      script.remove()
      reject(new Error(message))
    }
    const timer = setTimeout(() => fail('百度地图加载超时，请检查网络、浏览器端 AK 和 Referer 白名单后刷新页面。'), 15000)
    callbacks[callbackName] = () => {
      if (settled) return
      const sdk = window.BMap
      if (!sdk || typeof sdk.Map !== 'function' || typeof sdk.Point !== 'function') {
        fail('百度地图未能初始化，请检查浏览器端应用是否启用 JavaScript API。')
        return
      }
      settled = true
      cleanup()
      resolve(sdk)
    }
    const url = new URL('https://api.map.baidu.com/api')
    url.search = new URLSearchParams({ v: '4.0', ak: key, callback: callbackName }).toString()
    script.src = url.href
    script.async = true
    script.onerror = () => fail('百度地图脚本加载失败，请检查网络和浏览器设置后刷新页面。')
    try { document.head.appendChild(script) }
    catch { fail('百度地图脚本无法加载，请刷新页面重试。') }
  })
  // Deliberately cache failures too: no hidden automatic retries or extra quota.
  request = { ak: key, promise }
  return promise
}
