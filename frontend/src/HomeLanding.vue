<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { createGlobePoints, projectGlobePoint } from './homeParticles.ts'

withDefaults(defineProps<{ hasSavedReport?: boolean }>(), { hasSavedReport: false })
defineEmits<{ start: []; resume: [] }>()

const globeCanvas = ref<HTMLCanvasElement | null>(null)
const titleCanvas = ref<HTMLCanvasElement | null>(null)
const titleReady = ref(false)
const reducedMotion = ref(false)
const motionEnabled = computed(() => !reducedMotion.value)
type Footprint = { id: number; x: number; y: number; angle: number; left: boolean; created: number }
const footprints = ref<Footprint[]>([])
let footprintId = 0
let previousStep: { x: number; y: number } | null = null
type Particle = { x: number; y: number; ox: number; oy: number; vx: number; vy: number }
const globe = createGlobePoints().map(point => ({ ...point, ox: 0, oy: 0, vx: 0, vy: 0 }))
const globeBeacons = globe.filter((_, index) => index % 713 === 0)
let letters: Particle[] = []
let globeContext: CanvasRenderingContext2D | null = null
let titleContext: CanvasRenderingContext2D | null = null
let globeSize = { width: 0, height: 0 }
let titleSize = { width: 0, height: 0 }
let observer: ResizeObserver | undefined
let preference: MediaQueryList | undefined
let frame = 0
let lastDraw = 0
let mounted = false
let angle = -105 * Math.PI / 180
let yawOffset = 0
let pitchOffset = 0
const globePointer = { x: 0, y: 0, active: false }
const titlePointer = { x: 0, y: 0, active: false }

function sizeCanvas(canvas: HTMLCanvasElement) {
  const bounds = canvas.getBoundingClientRect()
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  canvas.width = Math.round(bounds.width * dpr)
  canvas.height = Math.round(bounds.height * dpr)
  canvas.getContext('2d')?.setTransform(dpr, 0, 0, dpr, 0, 0)
  return { width: bounds.width, height: bounds.height }
}

function sampleTitle() {
  if (!titleContext || !titleSize.width || !titleSize.height) return
  const sample = document.createElement('canvas')
  sample.width = Math.ceil(titleSize.width)
  sample.height = Math.ceil(titleSize.height)
  const context = sample.getContext('2d', { willReadFrequently: true })
  if (!context) return
  const font = Math.min(108, titleSize.width / 3.08, titleSize.height / 1.2)
  context.font = `600 ${font}px "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif`
  context.textBaseline = 'top'
  context.fillStyle = '#fff'
  context.fillText('生活圈', 0, 3)
  const pixels = context.getImageData(0, 0, sample.width, sample.height).data
  const step = Math.max(3.1, font / 26)
  letters = []
  for (let y = step / 2; y < sample.height; y += step) {
    for (let x = step / 2; x < sample.width; x += step) {
      if ((pixels[(Math.floor(y) * sample.width + Math.floor(x)) * 4 + 3] ?? 0) < 140) continue
      letters.push({ x, y, ox: 0, oy: 0, vx: 0, vy: 0 })
    }
  }
  titleReady.value = letters.length > 0
}

function resize() {
  if (!mounted) return
  if (globeCanvas.value) globeSize = sizeCanvas(globeCanvas.value)
  if (titleCanvas.value) {
    titleSize = sizeCanvas(titleCanvas.value)
    sampleTitle()
  }
  draw(1, false)
}

function pushParticle(particle: Particle, x: number, y: number,
  pointer: typeof globePointer, radius: number, strength: number, delta: number, interactive: boolean) {
  if (!interactive) {
    particle.ox = particle.oy = particle.vx = particle.vy = 0
    return
  }
  const dx = x + particle.ox - pointer.x, dy = y + particle.oy - pointer.y
  const distance = Math.hypot(dx, dy)
  if (pointer.active && distance < radius) {
    const force = (1 - distance / radius) ** 2 * strength
    particle.vx += dx / Math.max(1, distance) * force * delta
    particle.vy += dy / Math.max(1, distance) * force * delta
  }
  particle.vx = (particle.vx - particle.ox * .045 * delta) * .77 ** delta
  particle.vy = (particle.vy - particle.oy * .045 * delta) * .77 ** delta
  particle.ox += particle.vx * delta
  particle.oy += particle.vy * delta
}

function drawMesh(context: CanvasRenderingContext2D, cx: number, cy: number, radius: number, yaw: number, pitch: number) {
  context.strokeStyle = 'rgba(151, 207, 244, .075)'
  context.lineWidth = .65
  const line = (latitude: number | null, longitude: number | null) => {
    context.beginPath()
    let connected = false
    for (let i = 0; i <= 96; i++) {
      const lat = latitude ?? (-Math.PI / 2 + Math.PI * i / 96)
      const lng = longitude ?? (Math.PI * 2 * i / 96)
      const point = projectGlobePoint({ x: Math.cos(lat) * Math.sin(lng), y: Math.sin(lat), z: Math.cos(lat) * Math.cos(lng), size: 1 }, yaw, pitch)
      if (point.z < 0) { connected = false; continue }
      const x = cx + point.x * radius, y = cy - point.y * radius
      if (connected) context.lineTo(x, y)
      else context.moveTo(x, y)
      connected = true
    }
    context.stroke()
  }
  for (let lat = -60; lat <= 60; lat += 30) line(lat * Math.PI / 180, null)
  for (let lng = 0; lng < 360; lng += 30) line(null, lng * Math.PI / 180)
}

function drawOrbits(context: CanvasRenderingContext2D, cx: number, cy: number, radius: number) {
  context.save()
  context.translate(cx, cy)
  context.rotate(-.38)
  for (const [index, scale] of [1.16, 1.29].entries()) {
    const rx = radius * scale, ry = radius * (.34 + index * .09)
    context.lineWidth = index ? .6 : .8
    context.strokeStyle = index ? 'rgba(134, 190, 221, .11)' : 'rgba(168, 220, 255, .2)'
    context.beginPath(); context.ellipse(0, 0, rx, ry, 0, 0, Math.PI * 2); context.stroke()
    const phase = angle * (index ? -.7 : 1.3) + index * 2.8
    const x = Math.cos(phase) * rx, y = Math.sin(phase) * ry
    context.fillStyle = '#d1f1ff'
    context.shadowColor = '#92d6ff'; context.shadowBlur = 12
    context.beginPath(); context.arc(x, y, index ? 1.7 : 2.5, 0, Math.PI * 2); context.fill()
    context.shadowBlur = 0
    context.strokeStyle = 'rgba(187, 231, 255, .6)'
    context.beginPath(); context.ellipse(0, 0, rx, ry, 0, phase - .2, phase); context.stroke()
  }
  context.restore()
}

function draw(delta: number, interactive: boolean) {
  if (globeContext && globeSize.width && globeSize.height) {
    const context = globeContext
    const { width, height } = globeSize
    const cx = width / 2, cy = height / 2
    const radius = Math.min(width, height) * .405
    const targetYaw = interactive && globePointer.active ? (globePointer.x / width - .5) * .12 : 0
    const targetPitch = interactive && globePointer.active ? (globePointer.y / height - .5) * .07 : 0
    yawOffset += (targetYaw - yawOffset) * .05 * delta
    pitchOffset += (targetPitch - pitchOffset) * .05 * delta
    const yaw = angle + yawOffset, pitch = -.17 + pitchOffset
    context.clearRect(0, 0, width, height)
    const atmosphere = context.createRadialGradient(cx, cy, radius * .88, cx, cy, radius * 1.15)
    atmosphere.addColorStop(0, 'rgba(83, 158, 210, 0)')
    atmosphere.addColorStop(.48, 'rgba(73, 147, 201, .13)')
    atmosphere.addColorStop(.65, 'rgba(140, 211, 255, .06)')
    atmosphere.addColorStop(1, 'rgba(73, 147, 201, 0)')
    context.fillStyle = atmosphere
    context.beginPath(); context.arc(cx, cy, radius * 1.15, 0, Math.PI * 2); context.fill()
    const shade = context.createRadialGradient(cx - radius * .3, cy - radius * .35, radius * .05, cx, cy, radius)
    shade.addColorStop(0, 'rgba(18, 49, 72, .6)')
    shade.addColorStop(.72, 'rgba(8, 26, 43, .78)')
    shade.addColorStop(1, 'rgba(4, 16, 29, .18)')
    context.fillStyle = shade
    context.beginPath(); context.arc(cx, cy, radius, 0, Math.PI * 2); context.fill()
    drawMesh(context, cx, cy, radius, yaw, pitch)
    context.strokeStyle = 'rgba(163, 215, 249, .2)'
    context.lineWidth = 1
    context.beginPath(); context.arc(cx, cy, radius, 0, Math.PI * 2); context.stroke()
    context.strokeStyle = 'rgba(131, 189, 224, .08)'
    context.beginPath(); context.arc(cx, cy, radius * 1.036, 0, Math.PI * 2); context.stroke()
    context.fillStyle = '#bde9ff'
    for (const point of globe) {
      const p = projectGlobePoint(point, yaw, pitch)
      const x = cx + p.x * radius, y = cy - p.y * radius
      pushParticle(point, x, y, globePointer, 105, .55, delta, interactive && p.z > .1)
      context.globalAlpha = p.z < 0 ? .045 : .16 + p.z ** .7 * .72
      const dotRadius = Math.max(.4, radius / 520) * point.size * (p.z < 0 ? .7 : 1)
      context.beginPath(); context.arc(x + point.ox, y + point.oy, dotRadius, 0, Math.PI * 2); context.fill()
    }
    context.globalAlpha = 1
    // A handful of geographic beacons adds depth without another particle system.
    for (const [index, point] of globeBeacons.entries()) {
      const p = projectGlobePoint(point, yaw, pitch)
      if (p.z < .2) continue
      const x = cx + p.x * radius, y = cy - p.y * radius
      const pulse = .5 + .5 * Math.sin(angle * 6 + index)
      const light = context.createRadialGradient(x, y, 0, x, y, 9 + pulse * 5)
      light.addColorStop(0, 'rgba(197, 235, 255, .65)')
      light.addColorStop(.2, 'rgba(135, 207, 255, .2)')
      light.addColorStop(1, 'rgba(135, 207, 255, 0)')
      context.fillStyle = light
      context.beginPath(); context.arc(x, y, 14, 0, Math.PI * 2); context.fill()
    }
    drawOrbits(context, cx, cy, radius)
  }
  if (titleContext && titleReady.value) {
    const context = titleContext
    context.clearRect(0, 0, titleSize.width, titleSize.height)
    const gradient = context.createLinearGradient(0, 0, titleSize.width * .8, titleSize.height)
    gradient.addColorStop(0, '#f1faff')
    gradient.addColorStop(.6, '#c7e9ff')
    gradient.addColorStop(1, '#8ac6ee')
    context.fillStyle = gradient
    const dotRadius = Math.max(.82, Math.min(1.3, titleSize.width / 330))
    context.beginPath()
    for (const point of letters) {
      pushParticle(point, point.x, point.y, titlePointer, 65, 1.15, delta, interactive)
      const x = point.x + point.ox, y = point.y + point.oy
      context.moveTo(x + dotRadius, y)
      context.arc(x, y, dotRadius, 0, Math.PI * 2)
    }
    context.fill()
  }
}

function animate(time: number) {
  frame = 0
  if (!mounted || !motionEnabled.value || document.hidden) return
  if (time - lastDraw >= 32) {
    const delta = Math.min((time - lastDraw) / 16.67 || 1, 3)
    lastDraw = time
    angle += delta * .0018
    draw(delta, true)
    if (footprints.value.length) footprints.value = footprints.value.filter(print => Date.now() - print.created < 1900)
  }
  frame = window.requestAnimationFrame(animate)
}

function syncAnimation() {
  if (frame) window.cancelAnimationFrame(frame)
  frame = 0
  lastDraw = 0
  if (!mounted || document.hidden || !motionEnabled.value) {
    footprints.value = []
    previousStep = null
    globePointer.active = titlePointer.active = false
    if (mounted && !document.hidden) draw(1, false)
    return
  }
  if (motionEnabled.value) frame = window.requestAnimationFrame(animate)
  else draw(1, false)
}

function movePointer(event: PointerEvent, pointer: typeof globePointer) {
  if (!motionEnabled.value || event.pointerType === 'touch') return
  const bounds = (event.currentTarget as HTMLCanvasElement).getBoundingClientRect()
  pointer.x = event.clientX - bounds.left
  pointer.y = event.clientY - bounds.top
  pointer.active = true
}
function leaveFootprints(event: PointerEvent) {
  if (!motionEnabled.value || event.pointerType === 'touch' || document.hidden) return
  const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const x = event.clientX - bounds.left, y = event.clientY - bounds.top
  if (!previousStep) { previousStep = { x, y }; return }
  const dx = x - previousStep.x, dy = y - previousStep.y
  const distance = Math.hypot(dx, dy)
  if (distance < 31) return
  const angle = Math.atan2(dy, dx)
  const steps = Math.min(4, Math.floor(distance / 31))
  for (let i = 1; i <= steps; i++) {
    const left = ++footprintId % 2 === 0
    const offset = left ? -8 : 8
    const along = distance > 160 ? distance : i * 31
    footprints.value.push({ id: footprintId, x: previousStep.x + dx / distance * along - Math.sin(angle) * offset,
      y: previousStep.y + dy / distance * along + Math.cos(angle) * offset,
      angle: angle * 180 / Math.PI + 90, left, created: Date.now() })
    if (distance > 160) break
  }
  footprints.value = footprints.value.slice(-22)
  previousStep = { x, y }
}
function updatePreference() { reducedMotion.value = preference?.matches ?? false }
watch(reducedMotion, syncAnimation)

onMounted(() => {
  mounted = true
  globeContext = globeCanvas.value?.getContext('2d') ?? null
  titleContext = titleCanvas.value?.getContext('2d') ?? null
  preference = window.matchMedia('(prefers-reduced-motion: reduce)')
  updatePreference()
  preference.addEventListener('change', updatePreference)
  document.addEventListener('visibilitychange', syncAnimation)
  window.addEventListener('resize', resize)
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(resize)
    if (globeCanvas.value) observer.observe(globeCanvas.value)
    if (titleCanvas.value) observer.observe(titleCanvas.value)
  }
  resize()
  syncAnimation()
  // System fonts only; redraw if the browser resolves its local Chinese fallback later.
  void document.fonts?.ready.then(() => { if (mounted) resize() })
})
onUnmounted(() => {
  mounted = false
  footprints.value = []
  previousStep = null
  if (frame) window.cancelAnimationFrame(frame)
  observer?.disconnect()
  preference?.removeEventListener('change', updatePreference)
  document.removeEventListener('visibilitychange', syncAnimation)
  window.removeEventListener('resize', resize)
})
</script>

<template>
  <section class="home-landing" :data-motion="reducedMotion ? 'reduced' : 'running'" aria-label="生活圈首页"
    @pointermove="leaveFootprints" @pointerleave="previousStep = null">
    <div class="footprint-trail" aria-hidden="true">
      <svg v-for="print in footprints" :key="print.id" class="walking-print" viewBox="0 0 18 36"
        :style="{ left: `${print.x}px`, top: `${print.y}px`, transform: `translate(-50%, -50%) rotate(${print.angle}deg) scaleX(${print.left ? -1 : 1})` }">
        <path d="M8 1C3 1 1 6 2 13c.4 4 3 7 7 7s6-3 6-7C16 6 13 1 8 1Zm-3 23-1 6c0 6 9 7 10 1l-1-7Z" />
      </svg>
    </div>
    <header class="landing-header">
      <div class="landing-brand"><span class="brand-orbit" aria-hidden="true"><i></i></span><span>生活圈<span class="brand-divider">/</span><small>LIFE CIRCLE</small></span></div>
      <span class="landing-header-note" aria-hidden="true">WALK · DISCOVER · BELONG</span>
    </header>

    <main class="landing-main">
      <div class="landing-globe" aria-hidden="true">
        <div class="globe-halo"></div>
        <canvas ref="globeCanvas" class="globe-canvas" @pointermove="movePointer($event, globePointer)" @pointerleave="globePointer.active = false"></canvas>
        <span class="globe-axis axis-top"></span><span class="globe-axis axis-bottom"></span>
        <span class="globe-caption">A CITY WITHIN REACH<span>步行之间，日常可达</span></span>
      </div>
      <div class="landing-copy">
        <p class="landing-eyebrow"><span></span>15-MINUTE LIFE CIRCLE</p>
        <h1 class="landing-title">
          <span class="title-semantic">15分钟生活圈</span>
          <span class="title-time" aria-hidden="true"><strong>15</strong><span>分钟<small>MINUTES</small></span><i></i></span>
          <span v-if="!titleReady" class="title-fallback" aria-hidden="true">生活圈</span>
          <canvas ref="titleCanvas" class="title-canvas" :class="{ 'title-ready': titleReady }" aria-hidden="true"
            @pointermove="movePointer($event, titlePointer)" @pointerleave="titlePointer.active = false"></canvas>
        </h1>
        <p class="landing-description">面向自然资源和规划主管部门，<br />辅助公共服务设施布局的前期排查。</p>
        <div class="landing-actions">
          <button class="landing-start" type="button" @click="$emit('start')"><span>开始体检<small>PLANNING FIELD REVIEW</small></span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h15m-6-6 6 6-6 6" /></svg></button>
          <button v-if="hasSavedReport" class="landing-resume" type="button" @click="$emit('resume')">打开上次报告<span aria-hidden="true">↗</span></button>
        </div>
      </div>
    </main>

    <footer class="landing-footer"><span>5 / 10 / 15 MIN <i>·</i> 7 类生活服务</span><span class="footer-line" aria-hidden="true"></span><span class="footer-note">规划前期调研</span></footer>
  </section>
</template>

<style scoped>
.home-landing { position: relative; isolation: isolate; display: flex; flex-direction: column; min-height: 100dvh; width: 100%; overflow: hidden; color: #e8f4ff; background: #060e1b; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }
.home-landing::before { position: absolute; inset: 0; z-index: -1; pointer-events: none; content: ''; background: radial-gradient(ellipse at 77% 43%, #15344b66, transparent 58%), linear-gradient(135deg, #0a1728 0%, #060e1b 55%, #050b16 100%); }
.footprint-trail { position: absolute; z-index: 5; inset: 0; overflow: hidden; pointer-events: none; }
.walking-print { position: absolute; width: 12px; height: 25px; fill: #c3e7fb; filter: drop-shadow(0 0 5px #93cfff66); animation: footprint-fade 1.9s ease-out both; }
@keyframes footprint-fade { 0% { opacity: .48; } 55% { opacity: .18; } 100% { opacity: 0; } }
.home-landing button { font: inherit; box-sizing: border-box; cursor: pointer; }
.landing-header { position: relative; z-index: 3; display: flex; align-items: center; justify-content: space-between; gap: 20px; width: 100%; padding: 32px clamp(24px, 5.1vw, 88px); }
.landing-brand { display: flex; align-items: center; gap: 13px; font-size: 15px; font-weight: 500; letter-spacing: .1em; }
.landing-brand > span:last-child { display: flex; align-items: center; gap: 15px; }
.landing-brand small { font-size: 9px; color: #95b2c8; letter-spacing: .22em; font-weight: 400; }
.brand-divider { color: #3b556d; font-weight: 300; }
.brand-orbit { position: relative; display: block; width: 23px; height: 23px; border: 1px solid #9bd7fb99; border-radius: 50%; }
.brand-orbit::after { content: ''; position: absolute; inset: 5px; border: 1px solid #a9ddff66; border-radius: 50%; }
.brand-orbit i { position: absolute; top: 0; right: 1px; width: 5px; height: 5px; border-radius: 50%; background: #c1ebff; box-shadow: 0 0 12px #a9ddff44; }
.landing-header-note { color: #6f8da5; font-size: 8px; letter-spacing: .2em; }
.landing-main { position: relative; display: flex; align-items: center; flex: 1; width: min(100%, 1600px); min-height: 540px; margin: 0 auto; padding: 32px clamp(28px, 7.4vw, 116px) 68px; box-sizing: border-box; }
.landing-copy { position: relative; z-index: 2; width: min(43vw, 510px); }
.landing-eyebrow { display: flex; align-items: center; gap: 12px; margin: 0 0 27px; color: #9cc2df; font-size: 10px; letter-spacing: .26em; line-height: 1.6; font-weight: 400; }
.landing-eyebrow > span { width: 22px; height: 1px; background: #b0e2ff; box-shadow: 0 0 8px #b0e2ff33; }
.landing-title { position: relative; display: block; width: 100%; height: clamp(285px, 25vw, 304px); margin: 0 0 20px; font-size: inherit; font-weight: 500; }
.title-time { display: flex; align-items: center; height: 155px; gap: 19px; color: #eef8ff; }
.title-time strong { font-family: "Helvetica Neue", Arial, sans-serif; font-size: clamp(112px, 11vw, 154px); font-weight: 250; letter-spacing: -.085em; line-height: 1; }
.title-time > span { display: grid; gap: 10px; padding-top: 27px; font-size: 26px; font-weight: 300; letter-spacing: .16em; color: #d1e3ee; }
.title-time small { font-size: 8px; font-weight: 400; letter-spacing: .21em; color: #7397b1; }
.title-time i { align-self: flex-end; width: 67px; height: 1px; margin: 0 0 30px 7px; background: linear-gradient(90deg, #77adcf88, transparent); }
.title-semantic { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; }
.title-canvas { display: block; width: 100%; height: 130px; opacity: 0; }
.title-canvas.title-ready { opacity: 1; }
.title-fallback { position: absolute; top: 158px; left: 0; font-size: clamp(72px, 7.5vw, 108px); line-height: 1.1; font-weight: 600; letter-spacing: .015em; color: #d4edff; pointer-events: none; }
.landing-description { margin: 0; color: #9eb4c9; font-size: 15px; letter-spacing: .06em; font-weight: 300; line-height: 1.9; }
.mobile-break { display: none; }
.landing-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 22px; margin-top: 37px; }
.landing-start { view-transition-name: life-entry; position: relative; isolation: isolate; display: flex; align-items: center; justify-content: space-between; gap: 27px; width: 254px; padding: 18px 25px; border: 1px solid #c7e7ff70; border-top-color: #e5f7ffb0; border-bottom-color: #95c7e744; border-radius: 50px; color: #e4f4ff; background: linear-gradient(125deg, #dbf0ff1c, #b7deff06 55%, #8bc9fc14); backdrop-filter: blur(22px) saturate(145%); -webkit-backdrop-filter: blur(22px) saturate(145%); box-shadow: inset 0 1px 0 #ffffff30, inset 0 -2px 9px #8cc8f711, 0 3px 25px #01081366, 0 0 22px #92ccff0a; font-size: 22px !important; letter-spacing: .13em; font-weight: 600; transition: transform .25s, box-shadow .25s, border-color .25s; }
.landing-start::before { position: absolute; z-index: -1; inset: -8px; content: ''; border-radius: inherit; background: linear-gradient(120deg, #d1efff10, transparent 45%, #91beff0c); filter: blur(12px); pointer-events: none; animation: entry-breathe 4s ease-in-out infinite; }
.landing-start::after { position: absolute; inset: -1px; content: ''; border-radius: inherit; padding: 1px; pointer-events: none; background: linear-gradient(110deg, transparent 32%, #ebfaffd9 49%, #9bcbff88 52%, transparent 68%) 150% 0 / 260% 100%; -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0); mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0); -webkit-mask-composite: xor; mask-composite: exclude; animation: entry-edge-light 6s ease-in-out infinite; }
@keyframes entry-breathe { 0%, 100% { opacity: .4; transform: scale(.98); } 50% { opacity: 1; transform: scale(1.07); } }
@keyframes entry-edge-light { 0%, 15% { background-position: 150% 0; opacity: .25; } 55% { opacity: .9; } 85%, 100% { background-position: -50% 0; opacity: .25; } }
.landing-start > span { display: grid; text-align: left; gap: 6px; }
.landing-start small { font-size: 7px; font-weight: 400; letter-spacing: .16em; color: #92b3ca; }
.landing-start svg { width: 25px; height: 25px; fill: none; stroke: #c7e9ff; stroke-width: 1.15; transition: transform .25s; }
.landing-start:hover { transform: translateY(-3px); border-color: #dbf4ffb8; box-shadow: inset 0 1px 0 #ffffff50, inset 0 -2px 12px #9bdcff1c, 0 10px 35px #01081366, 0 0 34px #92ccff20; }
.landing-start:hover svg { transform: translateX(4px); }
.landing-resume { display: flex; align-items: center; gap: 10px; width: auto; padding: 10px 0; border: 0; border-radius: 0; color: #90aec5; background: transparent; box-shadow: none; font-size: 11px !important; font-weight: 400; }
.landing-resume:hover { background: transparent; color: #d4efff; }
.landing-resume > span { font-size: 15px; }
.home-landing button:focus-visible { outline: 2px solid #afe0ff; outline-offset: 6px; }
.landing-globe { position: absolute; top: 50%; right: -11%; width: min(79vw, 1040px); aspect-ratio: 1; transform: translateY(-51%); }
.globe-canvas { position: relative; z-index: 1; display: block; width: 100%; height: 100%; touch-action: pan-y; }
.globe-halo { position: absolute; inset: 0; border-radius: 50%; background: radial-gradient(ellipse at 37% 30%, #5ba8cf13, transparent 55%), radial-gradient(circle, #12334e12 45%, #5da7de0b 59%, transparent 71%); pointer-events: none; }
.globe-caption { position: absolute; bottom: 2%; left: 40%; display: grid; gap: 10px; color: #7194ad; font-size: 7px; letter-spacing: .24em; pointer-events: none; }
.globe-caption > span { font-size: 10px; letter-spacing: .35em; font-weight: 300; color: #55778f; }
.globe-axis { position: absolute; left: 50%; width: 1px; height: 16px; background: #8fc6eb33; pointer-events: none; }
.axis-top { top: 6.2%; }.axis-bottom { bottom: 6.2%; }
.landing-footer { position: relative; z-index: 2; display: flex; align-items: center; gap: 24px; padding: 25px clamp(24px, 5.1vw, 88px) 30px; color: #66829a; font-size: 9px; letter-spacing: .12em; font-weight: 400; }
.landing-footer i { margin: 0 7px; font-style: normal; color: #6d9bb9; }
.footer-line { width: 72px; height: 1px; background: linear-gradient(90deg, #38577088, #38577000); }
.footer-note { color: #526e86; font-size: 10px; letter-spacing: .18em; }
@media (min-width: 1600px) { .landing-globe { right: -2%; }.landing-main { padding-left: 115px; } }
@media (max-width: 960px) { .landing-main { min-height: 570px; padding-left: 6vw; }.landing-copy { width: 48vw; }.landing-globe { right: -22%; width: 90vw; opacity: .8; }.landing-title { height: 270px; }.title-time { height: 144px; }.title-time strong { font-size: 132px; }.title-canvas { height: 118px; }.title-fallback { top: 148px; font-size: 94px; }.landing-description { font-size: 13px; }.landing-eyebrow { font-size: 9px; letter-spacing: .2em; } }
@media (max-width: 640px) {
  .landing-header { padding: 24px 23px; }.landing-brand { gap: 10px; font-size: 13px; }.landing-brand > span:last-child { gap: 10px; }.landing-brand small { font-size: 8px; letter-spacing: .13em; }.landing-header-note { display: none; }.brand-orbit { width: 21px; height: 21px; }
  .landing-main { flex-direction: column; align-items: stretch; justify-content: flex-start; min-height: 660px; padding: 42px 30px 24px; }
  .landing-copy { width: 100%; max-width: 390px; margin: 0 auto; }.landing-eyebrow { margin-bottom: 19px; font-size: 8px; letter-spacing: .24em; }
  .landing-title { width: 100%; max-width: 360px; height: 251px; margin-bottom: 17px; }.title-time { height: 142px; gap: 18px; }.title-time strong { font-size: 130px; }.title-time > span { font-size: 23px; }.title-time i { width: 38px; }.title-canvas { height: 105px; }.title-fallback { top: 145px; font-size: 86px; }.landing-description { font-size: 12px; color: #b1c6d8; letter-spacing: .06em; }
  .landing-actions { margin-top: 25px; gap: 18px; }.landing-start { width: 230px; padding: 16px 22px; font-size: 19px !important; }.landing-resume { font-size: 10px !important; }
  .landing-globe { top: auto; bottom: -119px; right: -75px; width: min(104vw, 490px); transform: none; opacity: .56; pointer-events: none; }.globe-axis, .globe-caption { display: none; }
  .landing-footer { padding: 24px 30px; gap: 17px; font-size: 8px; }.footer-line { width: 32px; }.footer-note { font-size: 9px; }
}
@media (max-width: 370px) { .landing-main { padding-left: 25px; padding-right: 25px; }.landing-title { height: 218px; }.title-time { height: 126px; }.title-time strong { font-size: 116px; }.title-time > span { font-size: 21px; }.title-canvas { height: 92px; }.title-fallback { top: 129px; font-size: 75px; }.landing-brand small, .brand-divider { display: none; }.landing-actions { gap: 12px; }.landing-resume { font-size: 9px !important; }.landing-start { width: 216px; } }
@media (hover: hover) and (pointer: fine) { .home-landing[data-motion="running"], .home-landing[data-motion="running"] button { cursor: none; } }
@media (prefers-reduced-motion: reduce) { .home-landing *, .home-landing *::before, .home-landing *::after { transition: none !important; animation: none !important; }.footprint-trail { display: none; }.landing-start:hover, .landing-start:hover svg { transform: none; } }
</style>
