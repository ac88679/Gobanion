<template>
  <header class="header">
    <div class="header-left">
      <span class="title">官弈</span>
      <span class="subtitle">Gobanion</span>
    </div>
    <div class="header-center">
      <span class="health" :class="{ connected: connected }">
        {{ connected ? '已连接' : '断开' }}
      </span>
    </div>
    <div class="header-right">
      <button class="btn btn-primary" @click="$emit('create')">新建</button>
    </div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ connected: boolean }>()
defineEmits<{ create: [] }>()
</script>

<style scoped>
.header {
  background: var(--surface);
  border-bottom: 1px solid var(--separator);
  padding: 12px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  position: sticky;
  top: 0;
  z-index: 20;
}
.header-left { display: flex; align-items: baseline; gap: 8px; }
.title { font-size: 18px; font-weight: 700; color: var(--text); }
.subtitle { font-size: 12px; color: var(--text-secondary); }
.header-center { position: absolute; left: 50%; transform: translateX(-50%); }
.health { font-size: 12px; color: var(--text-tertiary); }
.health.connected { color: var(--green); }
.btn {
  border: none; border-radius: var(--radius-sm);
  padding: 6px 16px; font-size: 14px; font-weight: 500;
  cursor: pointer; transition: all .15s;
}
.btn-primary {
  background: var(--accent); color: #fff;
}
.btn-primary:hover { background: var(--accent-hover); }
</style>
