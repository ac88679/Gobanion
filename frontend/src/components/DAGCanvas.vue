<template>
  <div class="canvas" ref="canvasRef">
    <div v-if="!nodes.length" class="empty">无节点</div>
    <svg ref="svgRef" v-show="nodes.length"></svg>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch, onUnmounted, nextTick } from 'vue'
import * as d3 from 'd3'
import type { DagNode } from '../types'

const props = defineProps<{ nodes: DagNode[] }>()
const emit = defineEmits<{ selectNode: [node: DagNode] }>()

const canvasRef = ref<HTMLElement>()
const svgRef = ref<SVGSVGElement>()

function getColor(status: string) {
  const m: Record<string, string> = {
    ready: '#007aff', running: '#ff9500',
    done: '#34c759', completed: '#34c759',
    failed: '#ff3b30', assigned: '#ff9500',
    pending: '#c6c6c8',
  }
  return m[status] || '#c6c6c8'
}

function toposort(nodes: DagNode[]): DagNode[] {
  const visited = new Set<string>()
  const result: DagNode[] = []
  const nodeMap = new Map(nodes.map(n => [n.node_id, n]))
  function dfs(id: string) {
    if (visited.has(id)) return
    visited.add(id)
    const n = nodeMap.get(id)
    if (n) { ;(n.dependencies || []).forEach(dfs); result.push(n) }
  }
  nodes.forEach(n => dfs(n.node_id))
  return result
}

function layout(nodes: DagNode[], W: number, H: number) {
  if (!nodes.length) return []
  const sorted = toposort(nodes)
  const levelOf = new Map<string, number>()
  const nodeMap = new Map(nodes.map(n => [n.node_id, n]))
  sorted.forEach(n => {
    const deps = n.dependencies || []
    levelOf.set(n.node_id, deps.length === 0 ? 0 : Math.max(...deps.filter(d => levelOf.has(d)).map(d => levelOf.get(d)!)) + 1)
  })
  const levels: string[][] = []
  levelOf.forEach((lv, id) => {
    if (!levels[lv]) levels[lv] = []
    levels[lv].push(id)
  })
  const nodeW = 150, nodeH = 52, padX = 40, padY = 60
  const result: any[] = []
  levels.forEach((ids, lv) => {
    const totalW = ids.length * nodeW + (ids.length - 1) * padX
    const startX = Math.max(10, (W - totalW) / 2)
    ids.forEach((id, i) => {
      const n = nodeMap.get(id)!
      result.push({
        id: n.node_id, title: n.title || '', goal: n.goal || '', status: n.status,
        x: startX + i * (nodeW + padX) + nodeW / 2,
        y: padY + lv * (nodeH + padY) + nodeH / 2,
      })
    })
  })
  return result
}

let ro: ResizeObserver | null = null

function draw() {
  if (!svgRef.value || !canvasRef.value) return
  const raw = props.nodes
  if (!raw.length) { d3.select(svgRef.value).selectAll('*').remove(); return }
  const W = canvasRef.value.clientWidth
  const H = canvasRef.value.clientHeight
  if (W <= 0 || H <= 0) return
  const svg = d3.select(svgRef.value)
  svg.selectAll('*').remove()
  svg.attr('viewBox', `0 0 ${W} ${H}`)
  const laid = layout(raw, W, H)
  const laidMap = new Map(laid.map(n => [n.id, n]))
  svg.append('defs').append('marker')
    .attr('id', 'arrow').attr('viewBox', '0 0 10 10')
    .attr('refX', 18).attr('refY', 5)
    .attr('markerWidth', 6).attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path').attr('d', 'M0,0 L10,5 L0,10').attr('fill', '#c6c6c8')
  const g = svg.append('g')
  raw.forEach(n => {
    const p = laidMap.get(n.node_id); if (!p) return
    ;(n.dependencies || []).forEach(dep => {
      const dp = laidMap.get(dep); if (!dp) return
      g.append('line')
        .attr('x1', dp.x).attr('y1', dp.y)
        .attr('x2', p.x).attr('y2', p.y)
        .attr('stroke', '#c6c6c8').attr('stroke-width', 1.5)
        .attr('stroke-dasharray', ['completed', 'done'].includes(p.status) ? 'none' : '4,2')
        .attr('marker-end', 'url(#arrow)')
    })
  })
  const nodesG = g.selectAll('g.node').data(laid).join('g').attr('class', 'node').style('cursor', 'pointer')
  nodesG.attr('transform', n => `translate(${n.x},${n.y})`)
  nodesG.append('rect').attr('width', 150).attr('height', 52).attr('x', -75).attr('y', -26)
    .attr('rx', 8).attr('fill', 'var(--surface)').attr('stroke', n => getColor(n.status)).attr('stroke-width', 1.5)
  nodesG.append('text').attr('text-anchor', 'middle').attr('y', -8)
    .attr('fill', 'var(--text)').attr('font-size', '12px')
    .text(n => {
      const label = n.title || n.goal || ''
      return label.length > 16 ? label.slice(0, 16) + '…' : label
    })
  nodesG.append('text').attr('text-anchor', 'middle').attr('y', 10)
    .attr('fill', 'var(--text-secondary)').attr('font-size', '10px')
    .text(n => n.status || '')
  nodesG.on('click', (event: any, d: any) => {
    const node = raw.find(r => r.node_id === d.id)
    if (node) emit('selectNode', { node, pos: { x: event.clientX, y: event.clientY } })
  })
  svg.call(d3.zoom<SVGSVGElement, unknown>().scaleExtent([0.3, 3]).on('zoom', (e) => g.attr('transform', e.transform)) as any)
}

onMounted(() => { nextTick(() => draw()); if (canvasRef.value) { ro = new ResizeObserver(() => draw()); ro.observe(canvasRef.value) } })
onUnmounted(() => ro?.disconnect())
watch(() => props.nodes, () => { nextTick(() => draw()) }, { deep: true })
</script>

<style scoped>
.canvas { flex: 1; position: relative; overflow: hidden; background: var(--bg); min-height: 0; }
.canvas svg { display: block; width: 100%; height: 100%; }
.empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-tertiary); font-size: 14px; }
</style>
