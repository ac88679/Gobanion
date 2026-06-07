<template>
  <NavBar :connected="connected" @create="showModal = true" />
  <div class="layout">
    <Sidebar
      :dags="dags"
      :selectedId="selectedDagId"
      @select="selectDag"
    />
    <div class="main">
      <div v-if="!selectedDag" class="empty-state">
        <div class="empty-icon">📋</div>
        <p>选择一个任务或新建一个</p>
      </div>
      <template v-else>
        <div class="detail-hd">
          <div>
            <h2>{{ selectedDag.title || selectedDag.goal || selectedDag.dag_id }}</h2>
            <StatusBadge :status="selectedDag.status" />
          </div>
        </div>
        <DAGCanvas
          :nodes="nodes"
          @selectNode="({ node, pos }) => { selectedNode = node; sheetPos = pos }"
        />
      </template>
    </div>
  </div>

  <CreateModal
    :show="showModal"
    :loading="planLoading"
    @close="showModal = false"
    @plan="handlePlan"
  />

  <NodeSheet
    :node="selectedNode"
    :pos="sheetPos"
    :dagId="selectedDagId"
    @close="selectedNode = null; sheetPos = null"
    @aborted="onNodeAborted"
  />
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import NavBar from './components/NavBar.vue'
import Sidebar from './components/Sidebar.vue'
import StatusBadge from './components/StatusBadge.vue'
import DAGCanvas from './components/DAGCanvas.vue'
import CreateModal from './components/CreateModal.vue'
import NodeSheet from './components/NodeSheet.vue'
import type { DagSummary, DagDetail, DagNode } from './types'
import { fetchHealth, fetchDAGs, fetchDAGDetail, createPlan } from './api'

// ── State ──
const connected = ref(false)
const dags = ref<DagSummary[]>([])
const selectedDagId = ref<string | null>(null)
const selectedDag = ref<DagDetail | null>(null)
const nodes = ref<DagNode[]>([])
const selectedNode = ref<DagNode | null>(null)
const sheetPos = ref<{ x: number; y: number } | null>(null)
const showModal = ref(false)
const planLoading = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null

// ── Load ──
async function loadAll() {
  try {
    const h = await fetchHealth()
    connected.value = h.status === 'ok'
  } catch { connected.value = false; return }

  try {
    dags.value = await fetchDAGs()
  } catch { /* ignore */ }
}

async function selectDag(id: string) {
  selectedDagId.value = id
  selectedNode.value = null
  try {
    const detail = await fetchDAGDetail(id)
    selectedDag.value = detail
    nodes.value = detail.nodes || []
    startPoll(id)
  } catch { selectedDag.value = null }
}

async function handlePlan(goal: string, context?: string) {
  planLoading.value = true
  try {
    const result = await createPlan({ goal, context })
    showModal.value = false
    await loadAll()
    if (result.dag_id) {
      await selectDag(result.dag_id)
    }
  } catch (e: any) {
    alert('规划失败: ' + (e?.response?.data?.detail || e?.message || '未知错误'))
  } finally {
    planLoading.value = false
  }
}

function onNodeAborted(_nodeId: string) {
  // Close sheet and refresh
  selectedNode.value = null
  sheetPos.value = null
  if (selectedDagId.value) {
    // Force a refresh on next poll cycle
    startPoll(selectedDagId.value)
  }
}

// ── Polling ──
function startPoll(dagId: string) {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = setInterval(async () => {
    const cur = dags.value.find(d => d.dag_id === dagId)
    if (!cur || cur.status === 'completed' || cur.status === 'failed') {
      if (pollTimer) clearInterval(pollTimer)
      return
    }
    try {
      const detail = await fetchDAGDetail(dagId)
      selectedDag.value = detail
      nodes.value = detail.nodes || []
      // Update summary status
      const idx = dags.value.findIndex(d => d.dag_id === dagId)
      if (idx >= 0) dags.value[idx].status = detail.status
    } catch { /* ignore */ }
  }, 3000)
}

onMounted(async () => {
  await loadAll()
  if (dags.value.length > 0) await selectDag(dags.value[0].dag_id)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.layout {
  display: flex; flex: 1; overflow: hidden;
  height: calc(100vh - 50px);
}
.main {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
}
.empty-state {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  color: var(--text-tertiary); gap: 4px;
}
.empty-icon { font-size: 48px; }
.empty-state p { font-size: 14px; }
.detail-hd {
  padding: 14px 20px;
  border-bottom: 1px solid var(--separator);
  background: var(--surface);
  display: flex; align-items: center; gap: 10px;
}
.detail-hd h2 { font-size: 16px; font-weight: 600; margin: 0; }
</style>
