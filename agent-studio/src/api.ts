export type Agent = {
  id: string; name: string; description: string; role: string;
  instructions: string; model: string; temperature: number | null; accent: string;
};
export type TeamMember = Agent & { team_role: string; x: number; y: number };
export type Edge = { id: string; source_agent_id: string; target_agent_id: string; kind: string };
export type Project = {
  id: string; name: string; objective: string; state: string; max_revisions: number;
  team_size?: number; artifact_count?: number; pending_count?: number;
};
export type Cycle = {
  id: string; status: string; brief: string; cursor: number; step_count: number;
  plan: any[]; revisions: number; demo: boolean; note: string;
};
export type Message = {
  id: number; agent_name: string; kind: string; depth: number;
  title: string; body: string; payload: any; created_at: string;
};
export type Decision = { id: string; title: string; detail: string; status: string; payload?: any };
export type Summary = { running: number; paused: number; pending: number };
export type Artifact = { id: string; agent_name: string; type: string; title: string; body: string };

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) throw new Error((await res.text()).slice(0, 300));
  return res.json();
}

export const api = {
  state: () => call<{ db: boolean; db_detail: string; live: boolean; mode: string }>("/api/state"),
  summary: () => call<Summary>("/api/summary"),
  seed: () => call<any>("/api/seed", { method: "POST" }),

  agents: () => call<Agent[]>("/api/agents"),
  createAgent: (b: Partial<Agent>) => call<Agent>("/api/agents", { method: "POST", body: JSON.stringify(b) }),
  updateAgent: (id: string, b: Partial<Agent>) =>
    call<Agent>(`/api/agents/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  deleteAgent: (id: string) => call<any>(`/api/agents/${id}`, { method: "DELETE" }),
  memory: (id: string) => call<{ id: string; content: string }[]>(`/api/agents/${id}/memory`),
  addMemory: (id: string, content: string) =>
    call<any>(`/api/agents/${id}/memory`, { method: "POST", body: JSON.stringify({ content }) }),
  delMemory: (id: string) => call<any>(`/api/memory/${id}`, { method: "DELETE" }),

  projects: () => call<Project[]>("/api/projects"),
  createProject: (b: Partial<Project>) =>
    call<Project>("/api/projects", { method: "POST", body: JSON.stringify(b) }),
  deleteProject: (id: string) => call<any>(`/api/projects/${id}`, { method: "DELETE" }),
  project: (id: string) =>
    call<{ project: Project; team: TeamMember[]; edges: Edge[]; cycle: Cycle | null }>(`/api/projects/${id}`),
  setTeam: (id: string, members: { agent_id: string; role: string; x: number; y: number }[]) =>
    call<any>(`/api/projects/${id}/team`, { method: "PUT", body: JSON.stringify(members) }),
  setEdges: (id: string, edges: { source: string; target: string; kind: string }[]) =>
    call<any>(`/api/projects/${id}/edges`, { method: "PUT", body: JSON.stringify(edges) }),
  suggestions: (id: string) =>
    call<{ agent: Agent; reason: string }[]>(`/api/projects/${id}/suggestions`),

  startCycle: (id: string, brief: string) =>
    call<Cycle>(`/api/projects/${id}/cycles`, { method: "POST", body: JSON.stringify({ brief }) }),
  control: (id: string, action: string, text = "") =>
    call<any>(`/api/projects/${id}/control`, { method: "POST", body: JSON.stringify({ action, text }) }),
  stream: (id: string, after: number) =>
    call<{ messages: Message[]; cycle: Cycle | null; project: Project; decisions: Decision[]; artifacts: Artifact[] }>(
      `/api/projects/${id}/stream?after=${after}`,
    ),
  respond: (id: string, status: string, response = "") =>
    call<any>(`/api/decisions/${id}/respond`, { method: "POST", body: JSON.stringify({ status, response }) }),
  tick: () => call<{ did: string }>("/api/tick", { method: "POST" }),
};

export const ACCENTS: Record<string, string> = {
  violet: "#7c6ce0", sky: "#3d8fd4", amber: "#c98a2b",
  emerald: "#2e9c74", rose: "#c9566a", slate: "#7b7a75",
};
export const accentOf = (a?: string) => ACCENTS[a || "slate"] || ACCENTS.slate;
