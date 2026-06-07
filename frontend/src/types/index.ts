// ── DAG ──
export interface DagSummary {
  dag_id: string
  title: string
  goal?: string
  status: DagStatus
  created_at: string
  updated_at?: string
  _node_count?: number
}

export interface DagDetail extends DagSummary {
  nodes: DagNode[]
}

export type DagStatus = 'planning' | 'running' | 'completed' | 'failed'

// ── Node ──
export interface DagNode {
  node_id: string
  dag_id: string
  title?: string
  goal: string
  error?: string | null
  assigned_roles: string[]
  required_skills: string[]
  dependencies: string[]
  acceptance_criteria: string
  status: NodeStatus
  assigned_agents: Record<string, string>
  channel_id: string | null
  outputs: string[]
  self_check?: Record<string, unknown>[] | null
  created_at: string
  updated_at: string
}

export type NodeStatus =
  | 'pending' | 'ready' | 'assigned'
  | 'running' | 'done' | 'reviewing'
  | 'completed' | 'failed' | 'stuck' | 'interrupted'

// ── Plan request ──
export interface PlanRequest {
  goal: string
  context?: string
}

// ── Health ──
export interface HealthResponse {
  status: string
  service?: string
  version?: string
}
