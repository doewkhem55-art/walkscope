<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, useId, watch } from 'vue'
import { fitPanelSize, resizePanel } from './panelLayout.ts'
import type { PanelSize } from './panelLayout.ts'

const props = withDefaults(defineProps<{
  title: string
  subtitle?: string
  side: 'left' | 'right'
  initialWidth: number
  initialHeight: number
  drawerOpen?: boolean
  initialCollapsed?: boolean
  top?: number
  bottom?: number
}>(), { initialCollapsed: true, top: 12, bottom: 44, drawerOpen: undefined })
const emit = defineEmits<{ close: [] }>()
const contentId = useId()
const panel = ref<HTMLElement | null>(null)
const collapsed = ref(props.initialCollapsed)
const size = ref({ width: props.initialWidth, height: props.initialHeight })
const bounds = ref({ width: 1200, height: 800 })
const availableBounds = computed(() => ({ width: bounds.value.width, height: bounds.value.height - props.top - props.bottom + 56 }))
const displaySize = computed(() => {
  const fitted = fitPanelSize(size.value, availableBounds.value)
  const maxWidth = Math.max(280, Math.min(460, (bounds.value.width - 220) / 2))
  return { ...fitted, width: Math.min(fitted.width, maxWidth) }
})
const panelStyle = computed(() => ({
  width: collapsed.value ? '148px' : `${displaySize.value.width}px`,
  height: collapsed.value ? 'auto' : `${displaySize.value.height}px`,
  top: `${props.top}px`,
  '--panel-height': `${displaySize.value.height}px`,
}))
let observer: ResizeObserver | undefined
let drag: { x: number; y: number; size: PanelSize } | null = null

function toggleCollapsed() {
  collapsed.value = !collapsed.value
}
function expand() {
  collapsed.value = false
}
function beginResize(event: PointerEvent) {
  if (event.button !== 0 || collapsed.value) return
  drag = { x: event.clientX, y: event.clientY, size: { ...displaySize.value } }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function dragResize(event: PointerEvent) {
  if (!drag) return
  size.value = resizePanel(drag.size, event.clientX - drag.x, 0, props.side, availableBounds.value)
}
function endResize() { drag = null }
function keyboardResize(event: KeyboardEvent) {
  const dx = event.key === 'ArrowRight' ? 20 : event.key === 'ArrowLeft' ? -20 : 0
  const dy = 0
  if (!dx && !dy) return
  event.preventDefault()
  size.value = resizePanel(displaySize.value, dx, dy, props.side, availableBounds.value)
}
function measure() {
  const parent = panel.value?.parentElement
  if (parent) bounds.value = { width: parent.clientWidth, height: parent.clientHeight }
}
watch(() => props.drawerOpen, open => { if (open) collapsed.value = false })
onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined' && panel.value?.parentElement) {
    observer = new ResizeObserver(measure)
    observer.observe(panel.value.parentElement)
  }
  if (typeof window !== 'undefined') window.addEventListener('resize', measure)
})
onUnmounted(() => {
  observer?.disconnect()
  if (typeof window !== 'undefined') window.removeEventListener('resize', measure)
})
defineExpose({ expand })
</script>

<template>
  <aside ref="panel" class="panel workspace-panel" :class="[
    `panel-${side}`, { 'panel-collapsed': collapsed,
      'drawer-panel': drawerOpen !== undefined, 'drawer-visible': drawerOpen }]
    " :style="panelStyle" :aria-label="title">
    <header class="panel-heading">
      <div class="panel-heading-copy"><span v-if="subtitle" class="panel-subtitle">{{ subtitle }}</span><h2>{{ title }}</h2></div>
      <div class="panel-tools">
        <button class="panel-collapse" type="button" :aria-label="`${collapsed ? '展开' : '收起'}${title}`"
          :title="collapsed ? '展开' : '收起'" :aria-expanded="!collapsed" :aria-controls="contentId" @click="toggleCollapsed">{{ collapsed ? '+' : '−' }}</button>
        <button v-if="drawerOpen !== undefined" class="panel-drawer-close" type="button" :aria-label="`收起${title}面板`" @click="emit('close')">×</button>
      </div>
    </header>
    <div v-show="!collapsed" :id="contentId" class="panel-body"><slot /></div>
    <footer v-if="!collapsed && $slots.footer" class="panel-footer"><slot name="footer" /></footer>
    <button v-if="!collapsed" class="panel-resize" type="button" :aria-label="`调整${title}宽度`"
      title="左右拖动调整宽度；左右方向键每次调整20像素" @pointerdown.prevent="beginResize" @pointermove="dragResize"
      @pointerup="endResize" @pointercancel="endResize" @lostpointercapture="endResize" @keydown="keyboardResize"><span></span></button>
  </aside>
</template>

<style scoped>
.workspace-panel.panel { z-index: 8; bottom: auto !important; min-height: 0; max-width: calc(100% - 24px); max-height: calc(100% - 56px); display: flex; gap: 0; padding: 0; overflow: hidden; border-radius: 9px; box-shadow: 0 14px 36px #0006; }
.workspace-panel.panel-left { left: 12px; right: auto; }
.workspace-panel.panel-right { right: 12px; left: auto; }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex: none; padding: 10px 13px; min-height: 48px; border-bottom: 1px solid var(--line); background: transparent; }
.panel-heading-copy { min-width: 0; }
.panel-subtitle { display: block; font-size: 9px; letter-spacing: .12em; color: #7893a8; font-weight: 500; margin-bottom: 2px; }
.panel-heading h2 { margin: 0; font-size: 14px; font-weight: 600; line-height: 1.4; }
.panel-tools { display: flex; gap: 2px; flex: none; }
.panel-tools button { display: grid; place-content: center; width: 26px; height: 28px; padding: 0; border: 1px solid transparent; border-radius: 4px; color: #94a9bc; background: transparent; font-size: 19px; font-weight: 400; }
.panel-tools button:hover { color: #e8eef7; background: #ffffff0a; border-color: var(--line); }
.panel-body { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; display: flex; flex-direction: column; gap: 14px; padding: 14px 14px 22px; scrollbar-width: thin; scrollbar-color: #344858 transparent; }
.panel-body > :deep(*) { flex-shrink: 0; }
.panel-collapsed .panel-heading { border-bottom: 0; }
.panel-resize { position: absolute; top: 49px; bottom: 0; right: 0; width: 9px; height: auto; border: 0; padding: 0; border-radius: 0; color: #7b95ab; background: transparent; cursor: ew-resize; touch-action: none; font-size: 20px; }
.panel-right .panel-resize { left: 0; right: auto; cursor: ew-resize; }
.panel-footer { flex: none; padding: 12px 14px; border-top: 1px solid var(--line); }
.panel-resize:hover, .panel-resize:focus-visible { background: rgba(129, 206, 255, .22); }
.panel-tools .panel-drawer-close { display: none; }
@media (max-width: 960px) {
  .workspace-panel.panel { position: absolute; visibility: visible; transform: none; transition: none; }
  .workspace-panel.drawer-panel { position: fixed; top: auto !important; bottom: 65px !important; left: 8px; right: 8px; width: calc(100vw - 16px) !important; max-width: none; height: min(72dvh, var(--panel-height)) !important; max-height: calc(100dvh - 160px); z-index: 40; visibility: hidden; transform: translateY(calc(100% + 90px)); }
  .workspace-panel.drawer-visible { visibility: visible; transform: none; }
  .drawer-panel .panel-tools .panel-collapse, .drawer-panel .panel-resize { display: none; }
  .drawer-panel .panel-tools .panel-drawer-close { display: grid; }
}
</style>
