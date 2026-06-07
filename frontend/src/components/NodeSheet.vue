<template>
  <teleport to="body">
    <div class="backdrop" v-if="node" @click.self="close" @contextmenu.prevent="close" />
    <div class="float-card" v-if="node" :style="cardStyle" ref="cardRef">
      <button class="close-btn" @click="close">✕</button>
      <div class="card-content">
        <div class="field">
          <div class="label">标题</div>
          <div class="value">{{ node.title || node.goal?.slice(0, 40) + '…' }}</div>
        </div>
        <div class="field">
          <div class="label">目标</div>
          <div class="value">{{ node.goal }}</div>
        </div>
        <div class="field row">
          <div>
            <div class="label">状态</div>
            <StatusBadge :status="node.status" />
          </div>
          <div>
            <div class="label">角色</div>
            <div class="value">{{ node.assigned_roles?.join(', ') || '—' }}</div>
          </div>
        </div>
        <div class="field" v-if="canAbort">
          <button class="abort-btn" @click="handleAbort" :disabled="aborting">
            {{ aborting ? '中止中…' : '中止节点' }}
          </button>
        </div>
        <div class="field" v-if="node.required_skills?.length">
          <div class="label">技能</div>
          <div class="chips">
            <span v-for="s in node.required_skills" :key="s" class="chip">{{ s }}</span>
          </div>
        </div>
        <div class="field" v-if="node.dependencies?.length">
          <div class="label">依赖</div>
          <div class="value small">{{ node.dependencies.join(', ') }}</div>
        </div>
        <div class="field" v-if="node.acceptance_criteria">
          <div class="label">验收标准</div>
          <div class="value small">{{ node.acceptance_criteria }}</div>
        </div>
        <div class="field" v-if="node.outputs?.length">
          <div class="label">输出</div>
          <div class="value small">{{ node.outputs.join(', ') }}</div>
        </div>
        <div class="field error" v-if="node.error">
          <div class="label">失败原因</div>
          <div class="value error-text">{{ node.error }}</div>
        </div>
        <div class="field" v-if="node.self_check">
          <div class="label">自检</div>
          <div class="value small">{{ JSON.stringify(node.self_check) }}</div>
        </div>
      </div>
    </div>
  </teleport>
</template>

<script setup lang="ts">
import { computed, ref, nextTick, watch } from 'vue'
import type { DagNode } from '../types'
import { abortNode } from '../api'
import StatusBadge from './StatusBadge.vue'

const props = defineProps<{ node: DagNode | null; pos: { x: number; y: number } | null; dagId?: string }>()
const emit = defineEmits<{ close: []; aborted: [nodeId: string] }>()
const cardRef = ref<HTMLElement>()
const aborting = ref(false)

const canAbort = computed(() => props.node && ['assigned', 'running', 'ready'].includes(props.node.status))

async function handleAbort() {
  if (!props.node || !props.dagId || aborting.value) return
  aborting.value = true
  try {
    await abortNode(props.dagId, props.node.node_id)
    emit('aborted', props.node.node_id)
  } finally {
    aborting.value = false
  }
}

function close() { emit('close') }

const cardStyle = computed(() => {
  if (!props.pos) return { display: 'none' }
  return {
    position: 'fixed',
    left: props.pos.x + 16 + 'px',
    top: props.pos.y + 'px',
    zIndex: 40,
  } as any
})

watch(() => props.node, () => {
  if (!props.node) return
  nextTick(() => {
    const el = cardRef.value
    if (!el) return
    const rect = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight
    let { x, y } = props.pos!
    if (x + 16 + rect.width > vw) x = x - rect.width - 16
    if (y + rect.height > vh) y = vh - rect.height - 12
    el.style.left = Math.max(8, x) + 'px'
    el.style.top = Math.max(8, y) + 'px'
  })
})
</script>

<style scoped>
.abort-btn {
  width: 100%;
  padding: 8px 0;
  border: 1px solid #ff3b30;
  background: transparent;
  color: #ff3b30;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all .12s;
}
.abort-btn:hover:not(:disabled) {
  background: #fff0f0;
}
.abort-btn:disabled {
  opacity: .5;
  cursor: not-allowed;
}
</style>

<!-- rest of styles stay same -->

<style scoped>
.backdrop {
  position: fixed; inset: 0; z-index: 30;
}
.float-card {
  position: fixed;
  z-index: 40;
  width: 360px;
  max-height: 80vh;
  overflow-y: auto;
  background: var(--surface);
  border: 1px solid var(--separator);
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0,0,0,.18);
}
.close-btn {
  position: absolute; top: 8px; right: 10px;
  border: none; background: none; font-size: 16px;
  cursor: pointer; color: var(--text-secondary); line-height: 1;
  padding: 4px; border-radius: 4px;
}
.close-btn:hover { background: var(--surface-secondary); color: var(--text); }
.card-content { padding: 16px 20px 20px; }
.field { margin-bottom: 12px; }
.field.row { display: flex; gap: 24px; }
.label { font-size: 12px; color: var(--text-secondary); margin-bottom: 3px; }
.value { font-size: 14px; color: var(--text); }
.value.small { font-size: 12px; color: var(--text-secondary); }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip {
  background: var(--surface-secondary); color: var(--text-secondary);
  font-size: 11px; padding: 2px 8px; border-radius: 10px;
}
.field.error {
  background: #fff0f0; padding: 8px 10px; border-radius: 8px;
  border: 1px solid #ffd0d0;
}
.error-text { color: #d32f2f; font-size: 13px; white-space: pre-wrap; }
</style>
