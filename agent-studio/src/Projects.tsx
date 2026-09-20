import { useEffect, useState } from "react";
import { accentOf, api, type Agent, type Project } from "./api";

export function Projects({ onOpen, onCreateAgent, onLoadDemo, seeding }: {
  onOpen: (id: string) => void;
  onCreateAgent: () => void;
  onLoadDemo: () => void;
  seeding: boolean;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [objective, setObjective] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const load = () => Promise.all([api.projects(), api.agents()]).then(([p, a]) => { setProjects(p); setAgents(a); });
  useEffect(() => { load(); }, []);

  const start = () => {
    setName(""); setObjective("");
    setPicked(agents.filter((a) => a.role === "orchestrator").map((a) => a.id));
    setCreating(true);
  };

  const create = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const p = await api.createProject({ name, objective });
      const chosen = agents.filter((a) => picked.includes(a.id));
      const cols = Math.max(chosen.filter((a) => a.role === "specialist").length, 1);
      let i = 0;
      await api.setTeam(p.id, chosen.map((a) => {
        const role = a.role === "orchestrator" ? "orchestrator" : a.role === "critic" ? "critic" : "specialist";
        const pos = role === "orchestrator" ? { x: 400, y: 60 }
          : role === "critic" ? { x: 400, y: 420 }
          : { x: 140 + (i++ * (560 / Math.max(cols, 1))), y: 240 };
        return { agent_id: a.id, role, ...pos };
      }));
      const boss = chosen.find((a) => a.role === "orchestrator");
      if (boss) {
        await api.setEdges(p.id, chosen.filter((a) => a.id !== boss.id).map((a) => ({
          source: boss.id, target: a.id, kind: a.role === "critic" ? "reviews" : "calls",
        })));
      }
      setCreating(false);
      onOpen(p.id);
    } finally { setBusy(false); }
  };

  const remove = async (p: Project) => {
    if (!confirm(`Supprimer le projet « ${p.name} » et tout son historique ?`)) return;
    await api.deleteProject(p.id);
    load();
  };

  return (
    <>
      <div className="head">
        <div>
          <h1>Projets</h1>
          <p>Un projet réunit des agents autour d'un objectif et définit l'architecture qui dit lequel parle à lequel.</p>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={onCreateAgent}>+ Nouvel agent</button>
        <button className="btn primary" disabled={agents.length === 0} onClick={start}>Nouveau projet</button>
      </div>

      <div className="body">
        {projects.length === 0 ? (
          <div className="card"><div className="empty">
            {agents.length === 0 ? (
              <div style={{ display: "grid", gap: 14, justifyItems: "center" }}>
                <div>Aucun agent pour l'instant. Un projet a besoin d'agents pour fonctionner.</div>
                <button className="btn primary" onClick={onCreateAgent}>Créer mon premier agent</button>
                <div className="small" style={{ maxWidth: "52ch", lineHeight: 1.6 }}>
                  Vous préférez voir le fonctionnement avant de créer quoi que ce soit ?
                  L'exemple de démonstration crée d'un coup une équipe toute faite de 5 agents
                  (chef d'orchestre, veille, stratège, rédacteur, critique) et un projet qui les
                  relie, prêt à lancer un cycle.
                  <div style={{ marginTop: 9 }}>
                    <button className="btn sm" disabled={seeding} onClick={onLoadDemo}>
                      {seeding ? "chargement…" : "Charger l'exemple de démonstration"}
                    </button>
                  </div>
                </div>
              </div>
            ) : "Aucun projet. Créez-en un et choisissez son équipe."}
          </div></div>
        ) : (
          <div className="grid">
            {projects.map((p) => (
              <div className="card agent clickable" key={p.id} onClick={() => onOpen(p.id)}
                role="button" tabIndex={0}
                onKeyDown={(e) => { if (e.key === "Enter") onOpen(p.id); }}>
                <h3>{p.name}</h3>
                <p>{p.objective || <span className="muted">Pas d'objectif défini.</span>}</p>
                <div className="foot">
                  <span className={"tag " + (p.state === "RUNNING" ? "ok" : p.state === "PAUSED" ? "warn" : "")}>
                    {p.state === "RUNNING" ? "en cours" : p.state === "PAUSED" ? "en pause" : "au repos"}
                  </span>
                  <span className="tag">{p.team_size} agents</span>
                  {!!p.pending_count && <span className="tag warn">{p.pending_count} à arbitrer</span>}
                  <div style={{ flex: 1 }} />
                  <button className="btn sm ghost danger"
                    onClick={(e) => { e.stopPropagation(); remove(p); }}>Supprimer</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {creating && (
        <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && setCreating(false)}>
          <div className="modal">
            <div className="modal-h">Nouveau projet</div>
            <div className="modal-b">
              <label className="f">
                <span>Nom</span>
                <input className="f" value={name} placeholder="Contenu organique B2B" onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="f">
                <span>Objectif — il guide tout ce que l'équipe produit</span>
                <textarea className="f" rows={3} value={objective}
                  placeholder="Générer des leads B2B qualifiés grâce à du contenu organique, sans publicité."
                  onChange={(e) => setObjective(e.target.value)} />
              </label>
              <div className="small muted" style={{ margin: "14px 0 8px" }}>
                Choisissez les agents de l'équipe. L'architecture par défaut relie le chef d'orchestre à tous les autres ; vous pourrez la modifier ensuite.
              </div>
              {agents.map((a) => (
                <label key={a.id} className="row" style={{ padding: "6px 0", cursor: "pointer", alignItems: "flex-start" }}>
                  <input type="checkbox" checked={picked.includes(a.id)} style={{ marginTop: 4 }}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, a.id] : picked.filter((x) => x !== a.id))} />
                  <span className="dot" style={{ background: accentOf(a.accent), marginTop: 7 }} />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <b style={{ fontWeight: 600 }}>{a.name}</b>
                    <span className="small muted" style={{ display: "block" }}>{a.description}</span>
                  </span>
                  <span className="tag">{a.role === "orchestrator" ? "chef" : a.role === "critic" ? "critique" : "spécialiste"}</span>
                </label>
              ))}
            </div>
            <div className="modal-f">
              <button className="btn ghost" onClick={() => setCreating(false)}>Annuler</button>
              <button className="btn primary" disabled={busy || !name.trim() || picked.length === 0} onClick={create}>Créer le projet</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
