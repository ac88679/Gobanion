import axios from 'axios'
import type { DagSummary, DagDetail, PlanRequest, HealthResponse } from '../types'

const http = axios.create({
  baseURL: '/',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await http.get('/health')
  return data
}

export async function fetchDAGs(): Promise<DagSummary[]> {
  const { data } = await http.get('/api/v1/dag')
  return data.dags ?? data ?? []
}

export async function fetchDAGDetail(dagId: string): Promise<DagDetail> {
  const { data } = await http.get(`/api/v1/dag/${dagId}`)
  return data
}

export async function createPlan(req: PlanRequest): Promise<{ dag_id: string; title: string; nodes: unknown[] }> {
  const { data } = await http.post('/api/v1/plan', req)
  return data
}

export async function abortNode(dagId: string, nodeId: string): Promise<void> {
  await http.post(`/api/v1/dag/${dagId}/nodes/${nodeId}/abort`)
}

export async function abortDAG(dagId: string): Promise<{ aborted: number }> {
  const { data } = await http.post(`/api/v1/dag/${dagId}/abort`)
  return data
}
