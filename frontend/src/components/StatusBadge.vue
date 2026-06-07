<template>
  <span class="badge" :class="status">{{ status }}</span>
</template>

<script setup lang="ts">
import type { DagStatus, NodeStatus } from '../types'
defineProps<{ status: DagStatus | NodeStatus }>()
</script>

<style scoped>
.badge {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  line-height: 16px;
}
.planning { background: #e8e8ed; color: #8e8e93; }
.running { background: #d4edff; color: #007aff; }
.completed { background: #d1fae5; color: #16a34a; }
.failed { background: #ffe0df; color: #dc2626; }
.pending { background: #f3f3f7; color: #aeaeb2; }
.ready { background: #e8f4ff; color: #007aff; }
.assigned { background: #fff3e0; color: #f59e0b; }
.done { background: #d1fae5; color: #16a34a; }
.reviewing { background: #fef3c7; color: #d97706; }
.stuck { background: #fce4ec; color: #e53935; }
.interrupted { background: #e0e0e0; color: #757575; }
</style>
