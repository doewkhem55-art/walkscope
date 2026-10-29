<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { fitFloatingRect, moveFloatingRect, nextFloatingLayer, resizeFloatingRect } from './panelGeometry.ts'
import type { FloatingBounds, FloatingRect, ResizeEdge } from './panelGeometry.ts'

const props = withDefaults(defineProps<{
  label: string
  anchor?: 'center' | 'right' | 'left'
  dragHandle?: string
  minWidth?: number
  minHeight?: number
  disabled?: boolean
}>(), { anchor: 'left', dragHandle: ':scope > header', minWidth: 260, minHeight: 140, disabled: false })
const emit = defineEmits<{ resizeStart: [] }>()
const grips = ref<HTMLElement | null>(null)
const edges: ResizeEdge[] = ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']
const names = { n: '上边', ne: '右上角', e: '右边', se: '右下角', s: '下边', sw: '左下角', w: '左边', nw: '左上角' }
const primaryEdge = computed(() => props.anchor === 'right' ? 'sw' : 'se')
let pane: HTMLElement | null = null
let heading: HTMLElement | null = null
let touched = false
let headingStyle = { cursor: '', touchAction: '', title: '', tabindex: null as string | null, label: null as string | null }
let drag: { pane: HTMLElement; target: HTMLElement; pointerId: number; x: number; y: number; rect: FloatingRect; bounds: FloatingBounds; edge: ResizeEdge | 'move'; transition: string } | null = null

function boundsFor(element: HTMLElement): FloatingBounds {
  const parent = element.offsetParent as HTMLElement | null
  const rect = parent?.getBoundingClientRect()
  const left = Math.max(0, rect ? rect.left + parent!.clientLeft : 0)
  const top = Math.max(0, rect ? rect.top + parent!.clientTop : 0)
  const right = Math.min(window.innerWidth, rect && parent!.clientWidth ? rect.left + parent!.clientLeft + parent!.clientWidth : window.innerWidth)
  const bottom = Math.min(window.innerHeight, rect && parent!.clientHeight ? rect.top + parent!.clientTop + parent!.clientHeight : window.innerHeight)
  const margin = Math.min(12, Math.max(0, (right - left) / 4), Math.max(0, (bottom - top) / 4))
  return { left: left + margin, top: top + margin, right: Math.max(left + margin, right - margin), bottom: Math.max(top + margin, bottom - margin) }
}

function applyRect(element: HTMLElement, rect: FloatingRect, bounds: FloatingBounds, preserveHeight = false) {
  const height = element.style.height
  const parent = element.offsetParent as HTMLElement | null
  const origin = parent?.getBoundingClientRect()
  Object.assign(element.style, {
    left: `${rect.left - (origin?.left ?? 0) - (parent?.clientLeft ?? 0) + (parent?.scrollLeft ?? 0)}px`,
    top: `${rect.top - (origin?.top ?? 0) - (parent?.clientTop ?? 0) + (parent?.scrollTop ?? 0)}px`,
    right: 'auto', bottom: 'auto', transform: 'none',
    width: `${rect.width}px`, height: `${rect.height}px`, boxSizing: 'border-box',
    minWidth: `${Math.min(props.minWidth, rect.width, bounds.right - bounds.left)}px`,
    minHeight: `${Math.min(props.minHeight, rect.height, bounds.bottom - bounds.top)}px`,
    maxWidth: `${Math.max(0, bounds.right - bounds.left)}px`,
    maxHeight: `${Math.max(0, bounds.bottom - rect.top)}px`,
  })
  // Moving a collapsed window must retain its expanded or naturally sized height.
  if (preserveHeight) element.style.height = height
  touched = true
}

function raise() { if (pane) pane.style.zIndex = String(nextFloatingLayer()) }

function begin(element: HTMLElement, target: HTMLElement, edge: ResizeEdge | 'move', x: number, y: number, pointerId: number) {
  if (props.disabled) return
  stop()
  raise()
  const rect = element.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const bounds = boundsFor(element)
  drag = { pane: element, target, pointerId, x, y, edge, bounds, rect: fitFloatingRect(rect, bounds), transition: element.style.transition }
  element.style.transition = 'none'
  emit('resizeStart')
}

function start(event: PointerEvent, edge: ResizeEdge = primaryEdge.value) {
  if (event.button !== 0 || props.disabled) return
  const target = event.currentTarget as HTMLElement
  const element = grips.value?.parentElement ?? pane
  if (!element) return
  begin(element, target, edge, event.clientX, event.clientY, event.pointerId)
  if (drag) target.setPointerCapture(event.pointerId)
}
function startMove(event: PointerEvent) {
  if (event.button !== 0 || props.disabled || !pane) return
  const target = event.target as HTMLElement
  if (target.closest?.('button,a,input,textarea,select,summary,[role="button"],[data-no-drag]')) return
  event.preventDefault()
  begin(pane, event.currentTarget as HTMLElement, 'move', event.clientX, event.clientY, event.pointerId)
  if (drag) (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function change(dx: number, dy: number, snapDistance = 14) {
  if (!drag) return
  const next = drag.edge === 'move'
    ? moveFloatingRect(drag.rect, dx, dy, drag.bounds, snapDistance)
    : resizeFloatingRect(drag.rect, dx, dy, drag.edge, drag.bounds, { width: props.minWidth, height: props.minHeight })
  applyRect(drag.pane, next, drag.bounds, drag.edge === 'move')
}
function move(event: PointerEvent) {
  if (drag && event.pointerId === drag.pointerId) change(event.clientX - drag.x, event.clientY - drag.y)
}
function stop() {
  const current = drag
  drag = null
  if (!current) return
  current.pane.style.transition = current.transition
  if (current.target.hasPointerCapture?.(current.pointerId)) current.target.releasePointerCapture(current.pointerId)
}
function keyboard(event: KeyboardEvent, edge: ResizeEdge | 'move' = primaryEdge.value) {
  if (props.disabled || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
  if (edge === 'move' && event.target !== heading) return
  const element = grips.value?.parentElement ?? pane
  if (!element) return
  event.preventDefault()
  const step = event.shiftKey ? 5 : 20
  begin(element, event.currentTarget as HTMLElement, edge, 0, 0, -1)
  change(event.key === 'ArrowRight' ? step : event.key === 'ArrowLeft' ? -step : 0, event.key === 'ArrowDown' ? step : event.key === 'ArrowUp' ? -step : 0, 0)
  stop()
}
const keyboardMove = (event: KeyboardEvent) => keyboard(event, 'move')
function refit() {
  stop()
  if (!pane || !touched || props.disabled) return
  const rect = pane.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const bounds = boundsFor(pane)
  applyRect(pane, fitFloatingRect(rect, bounds), bounds, true)
}
function updateHeading() {
  if (!heading) return
  heading.style.cursor = props.disabled ? headingStyle.cursor : 'grab'
  heading.style.touchAction = props.disabled ? headingStyle.touchAction : 'none'
  if (props.disabled) {
    if (headingStyle.tabindex === null) heading.removeAttribute('tabindex')
    else heading.setAttribute('tabindex', headingStyle.tabindex)
  } else heading.setAttribute('tabindex', headingStyle.tabindex ?? '0')
}
watch(() => props.disabled, () => { stop(); updateHeading() })
onMounted(() => {
  pane = grips.value?.parentElement ?? null
  pane?.addEventListener('pointerdown', raise, true)
  pane?.addEventListener('focusin', raise)
  heading = pane?.querySelector<HTMLElement>(props.dragHandle) ?? null
  if (heading) {
    headingStyle = { cursor: heading.style.cursor, touchAction: heading.style.touchAction, title: heading.title, tabindex: heading.getAttribute('tabindex'), label: heading.getAttribute('aria-label') }
    heading.title = `${heading.title ? heading.title + '；' : ''}拖动移动窗口；方向键移动，Shift + 方向键微调`
    if (!headingStyle.label) heading.setAttribute('aria-label', `移动${props.label}窗口`)
    updateHeading()
    heading.addEventListener('pointerdown', startMove)
    heading.addEventListener('pointermove', move)
    heading.addEventListener('pointerup', stop)
    heading.addEventListener('pointercancel', stop)
    heading.addEventListener('lostpointercapture', stop)
    heading.addEventListener('keydown', keyboardMove)
  }
  window.addEventListener('resize', refit)
})
onUnmounted(() => {
  stop()
  window.removeEventListener('resize', refit)
  pane?.removeEventListener('pointerdown', raise, true)
  pane?.removeEventListener('focusin', raise)
  if (!heading) return
  heading.removeEventListener('pointerdown', startMove)
  heading.removeEventListener('pointermove', move)
  heading.removeEventListener('pointerup', stop)
  heading.removeEventListener('pointercancel', stop)
  heading.removeEventListener('lostpointercapture', stop)
  heading.removeEventListener('keydown', keyboardMove)
  heading.style.cursor = headingStyle.cursor
  heading.style.touchAction = headingStyle.touchAction
  heading.title = headingStyle.title
  if (headingStyle.tabindex === null) heading.removeAttribute('tabindex')
  else heading.setAttribute('tabindex', headingStyle.tabindex)
  if (headingStyle.label === null) heading.removeAttribute('aria-label')
  else heading.setAttribute('aria-label', headingStyle.label)
})
</script>

<template>
  <span ref="grips" class="resize-handles" style="position:absolute;inset:0;z-index:6;pointer-events:none" aria-label="窗口位置与大小">
    <button v-for="edge in edges" :key="edge" v-show="!disabled" type="button" class="resize-grip" :class="`edge-${edge}`"
      :tabindex="edge === primaryEdge ? 0 : -1" :aria-label="`拖动${names[edge]}调整${label}大小`"
      title="拖动边或角调整大小；方向键调整，Shift + 方向键微调"
      @pointerdown.stop.prevent="start($event, edge)" @pointermove="move" @pointerup="stop" @pointercancel="stop" @lostpointercapture="stop" @keydown="keyboard($event, edge)">
    </button>
  </span>
</template>

<style scoped>
.resize-grip{position:absolute!important;z-index:6;width:8px;height:8px;min-height:0!important;min-width:0!important;margin:0!important;padding:0!important;border:0!important;background:transparent!important;box-shadow:none!important;color:#c4e5fa70;touch-action:none;pointer-events:auto;border-radius:4px;font-size:20px;line-height:20px;transition:color .18s,background .18s}
.resize-grip:hover,.resize-grip:focus-visible{color:#f0faff;background:#c4e5fa22!important;outline-offset:-2px}
.edge-n,.edge-s{left:16px;right:16px;width:calc(100% - 32px);cursor:ns-resize}.edge-n{top:0}.edge-s{bottom:0}
.edge-e,.edge-w{top:16px;bottom:16px;height:calc(100% - 32px);cursor:ew-resize}.edge-e{right:0}.edge-w{left:0}
.edge-ne,.edge-se,.edge-sw,.edge-nw{width:18px;height:18px;display:grid;place-items:center}.edge-ne,.edge-sw{cursor:nesw-resize}.edge-nw,.edge-se{cursor:nwse-resize}
.edge-nw{left:0;top:0}.edge-ne{right:0;top:0}.edge-se{right:0;bottom:0}.edge-sw{left:0;bottom:0}
</style>
