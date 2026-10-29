<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import HomeLanding from './HomeLanding.vue'
import CheckupApp from './CheckupApp.vue'

const currentUrl = ref(new URL(window.location.href))
const isWorkspace = computed(() => currentUrl.value.searchParams.has('report') || currentUrl.value.searchParams.get('view') === 'map')
const lastWorkspaceUrl = ref<string | null>(isWorkspace.value ? currentUrl.value.href : null)
const hasSavedReport = computed(() => Boolean(lastWorkspaceUrl.value && new URL(lastWorkspaceUrl.value).searchParams.has('report')))

function syncLocation() {
  currentUrl.value = new URL(window.location.href)
  if (isWorkspace.value) lastWorkspaceUrl.value = currentUrl.value.href
}
function navigate(url: URL) {
  window.history.pushState(null, '', url)
  syncLocation()
}
function openWorkspace() {
  const url = new URL(currentUrl.value)
  url.searchParams.delete('report')
  url.searchParams.set('view', 'map')
  if (typeof document !== 'undefined' && document.startViewTransition && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.startViewTransition(() => navigate(url))
  } else navigate(url)
}
function resumeWorkspace() {
  if (lastWorkspaceUrl.value) navigate(new URL(lastWorkspaceUrl.value))
}
function openHome() {
  const url = new URL(currentUrl.value)
  url.searchParams.delete('report')
  url.searchParams.delete('view')
  navigate(url)
}
onMounted(() => window.addEventListener('popstate', syncLocation))
onUnmounted(() => window.removeEventListener('popstate', syncLocation))
</script>

<template>
  <div v-if="lastWorkspaceUrl" v-show="isWorkspace" class="experience-workspace">
    <CheckupApp :key="lastWorkspaceUrl" />
    <button type="button" class="home-link" aria-label="返回首页" @click="openHome"><span aria-hidden="true">←</span> 首页</button>
  </div>
  <HomeLanding v-if="!isWorkspace" :has-saved-report="hasSavedReport" @start="openWorkspace" @resume="resumeWorkspace" />
</template>

<style scoped>
.experience-workspace { position: relative; height: 100dvh; }
.home-link { position: absolute; top: 20px; left: max(60px, calc((100% - 1050px) / 2)); z-index: 25; display: flex; gap: 9px; align-items: center; width: auto; min-height: 32px; box-sizing: border-box; padding: 6px 13px; border: 1px solid #a5d6f522; border-radius: 20px; background: #08172566; backdrop-filter: blur(12px); color: #cae9fb; font-size: 11px; font-weight: 400; }
.home-link:hover { color: #fff; background: #16374c88; }
@media (max-width: 760px) { .home-link { left: 12px; transform: none; } }
</style>
