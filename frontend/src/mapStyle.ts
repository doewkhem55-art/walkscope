/**
 * Dark basemap strategy for Baidu JSAPI 4.0 (阶段4b).
 *
 * Browser evidence showed that `map.setMapStyleV2({ styleJson })` can return without
 * throwing while the SDK silently ignores the style (the current browser AK has no
 * personalised-map permission). Function existence is therefore NOT proof of success, so
 * the default is the CSS filter below: it deterministically darkens whatever the light
 * basemap renders, which is verifiable in a screenshot.
 *
 * Modes (build-time env, see .env.example):
 *   VITE_DARK_MAP_MODE=filter  (default)  CSS filter only; never claims custom style.
 *   VITE_DARK_MAP_MODE=custom             Try the SDK style, but only when the operator has
 *                                         explicitly confirmed it works for this AK via
 *                                         VITE_DARK_MAP_CUSTOM_CONFIRMED=true. Otherwise the
 *                                         verified filter is used instead.
 *   VITE_DARK_MAP_MODE=auto               Treated as `filter`: this project has no reliable
 *                                         runtime pixel proof for the SDK style, so auto must
 *                                         not claim `custom`.
 *
 * If a confirmed custom call is unavailable or throws, the result falls back to `filter`
 * and the view labels that truthfully.
 */

export type DarkMapMode = 'filter' | 'custom'

/**
 * The verified darkening filter applied to the Baidu tile canvas.
 *
 * `invert` + `hue-rotate` turns the light basemap into a dark navy map while keeping the
 * grey place/road labels readable; the extra brightness/contrast keeps land and water
 * separated and the reduced saturation mutes the provider's inverted POI icons. Scoped to
 * the tile canvas only, never to the report overlay or the panels.
 */
export const DARK_MAP_FILTER_CSS = 'invert(1) hue-rotate(180deg) brightness(.85) contrast(1.15) saturate(.5)'
export type DarkMapStyleMode = 'custom' | 'filter'

export interface DarkStyleTarget {
  setMapStyleV2?: (options: { styleJson: ReadonlyArray<Record<string, unknown>> }) => void
  setMapStyle?: (options: { styleJson: ReadonlyArray<Record<string, unknown>> }) => void
}

export interface DarkMapEnv {
  VITE_DARK_MAP_MODE?: string
  VITE_DARK_MAP_CUSTOM_CONFIRMED?: string
}

function confirmed(env: DarkMapEnv): boolean {
  const value = env.VITE_DARK_MAP_CUSTOM_CONFIRMED?.trim().toLowerCase()
  return value === 'true' || value === '1' || value === 'yes'
}

/**
 * Resolve the requested dark-map mode.
 *
 * Custom style is opt-in AND confirmation-gated because the SDK can silently ignore it.
 * Anything else (including `auto` and unknown values) resolves to the verifiable filter.
 */
export function resolveDarkMapMode(env: DarkMapEnv = {}): DarkMapMode {
  const requested = env.VITE_DARK_MAP_MODE?.trim().toLowerCase()
  if (requested === 'custom') return confirmed(env) ? 'custom' : 'filter'
  return 'filter'
}

/**
 * Baidu personalization JSON for an AK that does support personalised styles. Only used in
 * confirmed `custom` mode; kept bright enough for roads/labels on the dark basemap and hides
 * provider POI labels so report facilities are the only facility markers.
 */
export const DARK_MAP_STYLE_JSON: ReadonlyArray<Record<string, unknown>> = [
  { featureType: 'background', elementType: 'geometry', stylers: { color: '#0a1220' } },
  { featureType: 'land', elementType: 'geometry', stylers: { color: '#0f1a2b' } },
  { featureType: 'water', elementType: 'geometry', stylers: { color: '#071626' } },
  { featureType: 'green', elementType: 'geometry', stylers: { color: '#0d2030' } },
  { featureType: 'building', elementType: 'geometry', stylers: { color: '#141f31' } },
  { featureType: 'road', elementType: 'geometry', stylers: { color: '#243247' } },
  { featureType: 'highway', elementType: 'geometry', stylers: { color: '#38475f' } },
  { featureType: 'arterial', elementType: 'geometry', stylers: { color: '#2c3a51' } },
  { featureType: 'local', elementType: 'geometry', stylers: { color: '#202b3d' } },
  { featureType: 'railway', elementType: 'geometry', stylers: { color: '#1b2738' } },
  { featureType: 'subway', elementType: 'geometry', stylers: { color: '#1b2738' } },
  { featureType: 'boundary', elementType: 'geometry', stylers: { color: '#1d2a3c' } },
  { featureType: 'district', elementType: 'labels', stylers: { color: '#8296b0' } },
  { featureType: 'city', elementType: 'labels', stylers: { color: '#adc1da' } },
  { featureType: 'road', elementType: 'labels', stylers: { color: '#a3b5cc', visibility: 'on' } },
  { featureType: 'roadlabels', elementType: 'labels', stylers: { color: '#a3b5cc', visibility: 'on' } },
  { featureType: 'label', elementType: 'labels', stylers: { color: '#8ea3bd', visibility: 'on' } },
  { featureType: 'poi', elementType: 'labels', stylers: { visibility: 'off' } },
  { featureType: 'poilabel', elementType: 'labels', stylers: { visibility: 'off' } },
]

/**
 * Apply the requested strategy and return what the page must label as active.
 *
 * `filter` mode never touches the SDK. `custom` mode tries JSAPI 4.0 `setMapStyleV2`, then
 * the older `setMapStyle`; a missing method or a thrown error degrades to `filter`.
 */
export function applyDarkMapStyle(map: DarkStyleTarget, mode: DarkMapMode): DarkMapStyleMode {
  if (mode !== 'custom') return 'filter'
  const options = { styleJson: DARK_MAP_STYLE_JSON }
  for (const method of ['setMapStyleV2', 'setMapStyle'] as const) {
    const apply = map[method]
    if (typeof apply !== 'function') continue
    try {
      apply.call(map, options)
      return 'custom'
    } catch {
      // Try the other entry point, then fall back to the verifiable CSS filter.
    }
  }
  return 'filter'
}
