/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string
  readonly VITE_BAIDU_MAP_AK?: string
  /** Dark basemap mode: `filter` (default) or `custom` (needs explicit confirmation). */
  readonly VITE_DARK_MAP_MODE?: 'filter' | 'custom' | 'auto'
  /** Set to `true` only after visually confirming the AK really applies the custom style. */
  readonly VITE_DARK_MAP_CUSTOM_CONFIRMED?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
