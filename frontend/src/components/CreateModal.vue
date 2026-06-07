<template>
  <div class="overlay" v-if="show" @click.self="$emit('close')">
    <div class="modal">
      <h2>新建任务</h2>
      <div class="form-group">
        <label>任务描述</label>
        <textarea v-model="goal" placeholder="例如：搭建登录注册系统" rows="3"></textarea>
      </div>
      <div class="form-group">
        <label>额外上下文（可选）</label>
        <textarea v-model="context" placeholder="技术要求、约束等" rows="2"></textarea>
      </div>
      <div class="actions">
        <button class="btn btn-cancel" @click="$emit('close')">取消</button>
        <button class="btn btn-primary" :disabled="!goal.trim() || loading" @click="submit">
          {{ loading ? '规划中…' : '开始规划' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const emit = defineEmits<{ close: []; plan: [goal: string, context?: string] }>()
defineProps<{ show: boolean; loading: boolean }>()

const goal = ref('')
const context = ref('')

function submit() {
  if (!goal.value.trim()) return
  emit('plan', goal.value, context.value || undefined)
  goal.value = ''
  context.value = ''
}
</script>

<style scoped>
.overlay {
  position: fixed; inset: 0; z-index: 40;
  background: rgba(0,0,0,.4);
  display: flex; align-items: center; justify-content: center;
  padding: 20px;
}
.modal {
  background: var(--surface);
  border-radius: var(--radius);
  padding: 24px;
  width: 100%; max-width: 440px;
  box-shadow: var(--shadow);
}
.modal h2 { font-size: 18px; font-weight: 600; margin-bottom: 16px; }
.form-group { margin-bottom: 14px; }
.form-group label { display: block; font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
.form-group textarea {
  width: 100%; padding: 10px 12px;
  background: var(--surface-secondary);
  border: 1px solid var(--separator);
  border-radius: var(--radius-sm);
  color: var(--text); font-size: 14px; resize: vertical;
  outline: none; transition: border-color .15s;
}
.form-group textarea:focus { border-color: var(--accent); }
.actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px; }
.btn {
  border: none; border-radius: var(--radius-sm);
  padding: 8px 18px; font-size: 14px; font-weight: 500;
  cursor: pointer; transition: all .15s;
}
.btn:disabled { opacity: .5; cursor: not-allowed; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
.btn-cancel { background: var(--surface-secondary); color: var(--text); }
.btn-cancel:hover { background: var(--separator); }
</style>
