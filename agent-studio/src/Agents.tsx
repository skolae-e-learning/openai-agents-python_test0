import { useEffect, useState } from "react";
import { ACCENTS, accentOf, api, type Agent } from "./api";

const BLANK: Partial<Agent> = {
  name: "", description: "", role: "specialist", instructions: "",
  model: "gpt-4.1-mini", accent: "slate",
};

const ROLES: [string, string, string][] = [
  ["orchestrator", "Chef d'orchestre", "Décide du plan, arbitre, tranche. Un seul par projet."],
  ["specialist", "Spécialiste", "Produit une contribution dans son domaine."],
  ["critic", "Critique", "Évalue la production et peut la renvoyer en révision."],
];

// Persiste hors du cycle de vie React : la vue est remontée (clé `nonce`) après
// chaque sauvegarde, et ne doit pas rouvrir la création pour autant.
let consumedAutoOpen = 0;

export function Agents({ onChanged, autoOpenCreate, onLoadDemo, seeding }: {
  onChanged: () => void;
  autoOpenCreate?: number;
  onLoadDemo: () => void;
  seeding: boolean;
}) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [draft, setDraft] = useState<Partial<Agent> | null>(null);
  const [memory, setMemory] = useState<{ id: string; content: string }[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const load = () => api.agents().then(setAgents);
  useEffect(() => { load(); }, []);

  // Arrivée depuis un raccourci « Nouvel agent » ailleurs dans l'app : on ouvre
  // directement le formulaire de création, sans attendre un clic supplémentaire.
  useEffect(() => {
    if (autoOpenCreate && autoOpenCreate !== consumedAutoOpen) {
      consumedAutoOpen = autoOpenCreate;
      setDraft({ ...BLANK }); setNote(""); setMemory([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoOpenCreate]);

  const open = async (a?: Agent) => {
    setDraft(a ? { ...a } : { ...BLANK });
    setNote("");
    setMemory(a ? await api.memory(a.id) : []);
  };

  const save = async () => {
    if (!draft?.name?.trim()) return;
    setBusy(true);
    try {
      if (draft.id) await api.updateAgent(draft.id, draft);
      else await api.createAgent(draft);
      setDraft(null);
      await load();
      onChanged();
    } finally { setBusy(false); }
  };

  const remove = async (a: Agent) => {
    if (!confirm(`Supprimer « ${a.name} » ? Il sera retiré de tous les projets.`)) return;
    await api.deleteAgent(a.id);
    await load();
    onChanged();
  };

  const addNote = async () => {
    if (!draft?.id || !note.trim()) return;
    await api.addMemory(draft.id, note.trim());
    setNote("");
    setMemory(await api.memory(draft.id));
  };

  return (
    <>
      <div className="head">
        <div>
          <h1>Agents</h1>
          <p>Chaque agent a son nom, sa description, ses instructions, son modèle et sa mémoire propre. Un agent peut servir dans plusieurs projets.</p>
        </div>
        <div className="spacer" />
        <button className="btn primary" onClick={() => open()}>Nouvel agent</button>
      </div>

      <div className="body">
        {agents.length === 0 ? (
          <div className="card"><div className="empty">
            <div style={{ display: "grid", gap: 14, justifyItems: "center" }}>
              <div>Aucun agent pour l'instant.</div>
              <button className="btn primary" onClick={() => open()}>Créer mon premier agent</button>
              <div className="small" style={{ maxWidth: "52ch", lineHeight: 1.6 }}>
                L'exemple de démonstration crée d'un coup une équipe toute faite de 5 agents
                et un projet qui les relie, pour voir le fonctionnement sans rien écrire.
                <div style={{ marginTop: 9 }}>
                  <button className="btn sm" disabled={seeding} onClick={onLoadDemo}>
                    {seeding ? "chargement…" : "Charger l'exemple de démonstration"}
                  </button>
                </div>
              </div>
            </div>
          </div></div>
        ) : (
          <div className="grid">
            {agents.map((a) => (
              <div className="card agent clickable" key={a.id} onClick={() => open(a)}
                role="button" tabIndex={0}
                onKeyDown={(e) => { if (e.key === "Enter") open(a); }}>
                <h3>
                  <span className="dot" style={{ background: accentOf(a.accent) }} />
                  {a.name}
                </h3>
                <p>{a.description || <span className="muted">Pas de description.</span>}</p>
                <div className="foot">
                  <span className="tag">{ROLES.find((r) => r[0] === a.role)?.[1] || a.role}</span>
                  <span className="tag">{a.model}</span>
                  <div style={{ flex: 1 }} />
                  <button className="btn sm ghost danger"
                    onClick={(e) => { e.stopPropagation(); remove(a); }}>Supprimer</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {draft && (
        <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && setDraft(null)}>
          <div className="modal">
            <div className="modal-h">{draft.id ? "Modifier l'agent" : "Nouvel agent"}</div>
            <div className="modal-b">
              <label className="f">
                <span>Nom</span>
                <input className="f" value={draft.name || ""} placeholder="Rédacteur"
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              </label>
              <label className="f">
                <span>Description — ce que fait cet agent, en une phrase</span>
                <input className="f" value={draft.description || ""} placeholder="Produit le contenu à partir de l'angle retenu."
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
              </label>
              <label className="f">
                <span>Rôle dans une équipe</span>
                <select className="f" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
                  {ROLES.map(([v, l, h]) => <option key={v} value={v}>{l} — {h}</option>)}
                </select>
              </label>
              <label className="f">
                <span>Instructions — le comportement attendu</span>
                <textarea className="f" rows={5} value={draft.instructions || ""}
                  placeholder="Tu écris de façon dense et directe. Une idée par paragraphe…"
                  onChange={(e) => setDraft({ ...draft, instructions: e.target.value })} />
              </label>
              <div className="row" style={{ gap: 12 }}>
                <label className="f" style={{ flex: 1 }}>
                  <span>Modèle</span>
                  <input className="f" value={draft.model || ""} onChange={(e) => setDraft({ ...draft, model: e.target.value })} />
                </label>
                <label className="f" style={{ width: 150 }}>
                  <span>Couleur</span>
                  <select className="f" value={draft.accent} onChange={(e) => setDraft({ ...draft, accent: e.target.value })}>
                    {Object.keys(ACCENTS).map((k) => <option key={k} value={k}>{k}</option>)}
                  </select>
                </label>
              </div>

              {draft.id && (
                <div style={{ marginTop: 6 }}>
                  <div className="small muted" style={{ marginBottom: 6 }}>
                    Mémoire propre — ce que l'agent garde d'un cycle à l'autre et retrouve à chaque exécution.
                  </div>
                  {memory.map((m) => (
                    <div key={m.id} className="row small" style={{ padding: "5px 0", borderBottom: "1px solid var(--line-soft)" }}>
                      <span style={{ flex: 1 }}>{m.content}</span>
                      <button className="btn sm ghost" onClick={async () => { await api.delMemory(m.id); setMemory(await api.memory(draft.id!)); }}>×</button>
                    </div>
                  ))}
                  <div className="row" style={{ marginTop: 8 }}>
                    <input className="f" style={{ flex: 1 }} value={note} placeholder="Ajouter un souvenir durable…"
                      onChange={(e) => setNote(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && addNote()} />
                    <button className="btn" onClick={addNote}>Ajouter</button>
                  </div>
                </div>
              )}
            </div>
            <div className="modal-f">
              <button className="btn ghost" onClick={() => setDraft(null)}>Annuler</button>
              <button className="btn primary" disabled={busy || !draft.name?.trim()} onClick={save}>Enregistrer</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
