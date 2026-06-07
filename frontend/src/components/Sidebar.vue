<template>
  <div class="sidebar">
    <div class="section-hd">
      <span>任务列表</span>
      <span class="count">{{ dags.length }}</span>
    </div>
    <div class="list" ref="listRef">
      <div
        v-for="d in dags"
        :key="d.dag_id"
        class="item"
        :class="{ active: d.dag_id === selectedId }"
        @click="$emit('select', d.dag_id)"
      >
        <div class="item-title">{{ d.title || d.goal || d.dag_id.slice(0,12) }}</div>
        <div class="item-meta">
          <StatusBadge :status="d.status" />
          <span v-if="d._node_count !== undefined">{{ d._node_count }} 节点</span>
        </div>
      </div>
      <div v-if="!dags.length" class="empty">暂无任务</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { DagSummary } from '../types'
import StatusBadge from './StatusBadge.vue'

defineProps<{ dags: DagSummary[]; selectedId: string | null }>()
defineEmits<{ select: [id: string] }>()
</script>

<style scoped>
.sidebar {
  width: 260px; min-width: 260px;
  background: var(--surface-secondary);
  border-right: 1px solid var(--separator);
  display: flex; flex-direction: column;
  overflow: hidden;
}
.section-hd {
  padding: 14px 16px 10px;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  display: flex; justify-content: space-between;
}
.count {
  background: var(--separator); color: var(--text-secondary);
  border-radius: 10px; padding: 0 7px; font-size: 11px; line-height: 18px;
}
.list { flex: 1; overflow-y: auto; padding: 0 8px 8px; }
.item {
  background: var(--surface); border-radius: var(--radius-sm);
  padding: 10px 12px; margin-bottom: 4px;
  cursor: pointer; transition: all .12s;
  box-shadow: var(--shadow);
}
.item:hover { opacity: .8; }
.item.active { border-left: 3px solid var(--accent); }
.item-title { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
.item-meta { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-secondary); }
.empty { padding: 32px; text-align: center; color: var(--text-tertiary); font-size: 13px; }
</style>
