import * as auth from "./auth";

export type Agent = {
  id: string; name: string; description: string; role: string;
  instructions: string; model: string; temperature: number | null; accent: string;
  visibility?: string; owner_id?: string | null;
};
export type PublicAgent = Agent & { owner_label: string };
export type PublicProject = {
  id: string; name: string; objective: string; owner_label: string; team_size: number;
};
export type TeamMember = Agent & { team_role: string; x: number; y: number };
export type Edge = { id: string; source_agent_id: string; target_agent_id: string; kind: string };
export type Project = {
  id: string; name: string; objective: string; state: string; max_revisions: number;
  team_size?: number; artifact_count?: number; pending_count?: number;
  visibility?: string; owner_id?: string | null;
};
export type Cycle = {
  id: string; status: string; brief: string; cursor: number; step_count: number;
  plan: any[]; revisions: number; demo: boolean; note: string;
};
export type Message = {
  id: number; agent_name: string; kind: string; depth: number;
  title: string; body: string; payload: any; created_at: string;
};
export type Connector = {
  id: string; kind: string; label: string; name: string; capabilities: string[];
  config: Record<string, string>; secret_set: boolean;
  status: "untested" | "ok" | "error"; status_detail: string; last_tested_at: string | null;
};
export type ConnectorField = { key: string; label: string; required: boolean; placeholder?: string };
export type ConnectorKind = {
  kind: string; label: string; capabilities: string[]; secret_label: string;
  secret_required: boolean; fields: ConnectorField[]; help: string;
};
export type CycleImage = {
  id: string; filename: string; content_type: string; caption: string;
  created_at: string; url: string;
};
export type DecisionPayload = {
  reason?: { why?: string; agent?: string; action?: string };
  required_capabilities?: string[];
  connectors?: Connector[];
  missing?: string[];
};
export type Decision = {
  id: string; title: string; detail: string; status: string; payload?: DecisionPayload;
};
export type Summary = { running: number; paused: number; pending: number };
export type Artifact = { id: string; agent_name: string; type: string; title: string; body: string };

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  // Un envoi multipart doit laisser le navigateur poser lui-même le type et sa
  // frontière : un content-type imposé ici rendrait le corps illisible côté serveur.
  const isForm = init?.body instanceof FormData;
  const send = () =>
    fetch(path, {
      ...init,
      headers: {
        ...(isForm ? {} : { "content-type": "application/json" }),
        ...(auth.accessToken() ? { authorization: `Bearer ${auth.accessToken()}` } : {}),
        ...(init?.headers || {}),
      },
    });

  let res = await send();
  // Le jeton d'accès vit une heure : on le renouvelle une fois, en silence,
  // plutôt que d'éjecter l'utilisateur en plein travail.
  if (res.status === 401 && (await auth.refresh())) res = await send();

  if (!res.ok) {
    let message = (await res.text()).slice(0, 400);
    try {
      const parsed = JSON.parse(message);
      if (parsed?.detail) message = parsed.detail;
    } catch {
      /* message déjà lisible */
    }
    throw new Error(message);
  }
  return res.json();
}

export const api = {
  state: () => call<{ db: boolean; db_detail: string; live: boolean; mode: string;
                      connectors_ready: boolean; auth: auth.AuthConfig }>("/api/state"),
  summary: () => call<Summary>("/api/summary"),

  explore: (type: "agent" | "project", search: string, role: string) =>
    call<any[]>(`/api/explore?type=${type}&q_=${encodeURIComponent(search)}&role=${role}`),
  duplicateAgent: (id: string) => call<Agent>(`/api/agents/${id}/duplicate`, { method: "POST" }),
  duplicateProject: (id: string) => call<Project>(`/api/projects/${id}/duplicate`, { method: "POST" }),
  legacy: () => call<{ agents: number; projects: number }>("/api/legacy"),
  claimLegacy: () => call<{ agents: number; projects: number }>("/api/legacy/claim", { method: "POST" }),
  updateProject: (id: string, b: Partial<Project>) =>
    call<Project>(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
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
  respond: (id: string, status: string, response = "", connectors: string[] = []) =>
    call<any>(`/api/decisions/${id}/respond`, {
      method: "POST",
      body: JSON.stringify({ status, response, connectors }),
    }),
  tick: () => call<{ did: string }>("/api/tick", { method: "POST" }),

  connectorKinds: () =>
    call<{ ready: boolean; capabilities: string[]; kinds: ConnectorKind[] }>("/api/connectors/kinds"),
  connectors: () => call<Connector[]>("/api/connectors"),
  createConnector: (b: { kind: string; name: string; config: Record<string, string>; secret: string }) =>
    call<Connector>("/api/connectors", { method: "POST", body: JSON.stringify(b) }),
  updateConnector: (id: string, b: { kind: string; name: string; config: Record<string, string>; secret: string }) =>
    call<Connector>(`/api/connectors/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  deleteConnector: (id: string) => call<any>(`/api/connectors/${id}`, { method: "DELETE" }),
  testConnector: (id: string) => call<Connector>(`/api/connectors/${id}/test`, { method: "POST" }),

  cycleImages: (cycleId: string) => call<CycleImage[]>(`/api/cycles/${cycleId}/images`),
  addCycleImage: (cycleId: string, file: File, caption: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("caption", caption);
    return call<CycleImage>(`/api/cycles/${cycleId}/images`, { method: "POST", body: form });
  },
  deleteImage: (id: string) => call<any>(`/api/images/${id}`, { method: "DELETE" }),
};

export const ACCENTS: Record<string, string> = {
  violet: "#7c6ce0", sky: "#3d8fd4", amber: "#c98a2b",
  emerald: "#2e9c74", rose: "#c9566a", slate: "#7b7a75",
};
export const accentOf = (a?: string) => ACCENTS[a || "slate"] || ACCENTS.slate;
