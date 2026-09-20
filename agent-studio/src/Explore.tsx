import { useCallback, useEffect, useState } from "react";
import { accentOf, api } from "./api";

type Kind = "agent" | "project";

const ROLES: [string, string][] = [
  ["", "Tous les rôles"],
  ["orchestrator", "Chef d'orchestre"],
  ["specialist", "Spécialiste"],
  ["critic", "Critique"],
];

export function Explore({ onOpenProject, onChanged }: {
  onOpenProject: (id: string) => void;
  onChanged: () => void;
}) {
  const [kind, setKind] = useState<Kind>("agent");
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [copying, setCopying] = useState<string | null>(null);
  const [copied, setCopied] = useState<string>("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRows(await api.explore(kind, search, kind === "agent" ? role : ""));
    } finally {
      setLoading(false);
    }
  }, [kind, search, role]);

  // Recherche différée : on n'interroge pas l'API à chaque frappe.
  useEffect(() => {
    const t = setTimeout(load, search ? 350 : 0);
    return () => clearTimeout(t);
  }, [load, search]);

  const duplicate = async (row: any) => {
    setCopying(row.id);
    try {
      const copy = kind === "agent"
        ? await api.duplicateAgent(row.id)
        : await api.duplicateProject(row.id);
      setCopied(`« ${copy.name} » est maintenant dans vos ${kind === "agent" ? "agents" : "projets"}, en privé.`);
      onChanged();
    } catch (e: any) {
      setCopied(e?.message || "La duplication a échoué.");
    } finally {
      setCopying(null);
    }
  };

  return (
    <>
      <div className="head">
        <div>
          <h1>Explorer</h1>
          <p>
            Les agents et les projets que d'autres comptes ont rendus publics. Dupliquez-en un
            pour en obtenir votre propre copie, privée et modifiable, sans toucher à l'original.
          </p>
        </div>
      </div>

      <div className="body">
        <div className="row" style={{ marginBottom: 16, gap: 10 }}>
          <div className="seg">
            <button className={"btn sm" + (kind === "agent" ? " primary" : " ghost")}
              onClick={() => setKind("agent")}>Agents</button>
            <button className={"btn sm" + (kind === "project" ? " primary" : " ghost")}
              onClick={() => setKind("project")}>Projets</button>
          </div>
          <input className="f" style={{ flex: 1, minWidth: 200, maxWidth: 380 }}
            placeholder={kind === "agent" ? "Chercher un agent…" : "Chercher un projet…"}
            value={search} onChange={(e) => setSearch(e.target.value)} />
          {kind === "agent" && (
            <select className="f" style={{ width: 190 }} value={role}
              onChange={(e) => setRole(e.target.value)}>
              {ROLES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          )}
        </div>

        {copied && <div className="banner info" style={{ marginBottom: 14 }}>{copied}</div>}

        {loading ? (
          <div className="card"><div className="empty">Chargement…</div></div>
        ) : rows.length === 0 ? (
          <div className="card"><div className="empty">
            Rien de public ici pour l'instant.
            {search || role ? " Essayez d'élargir la recherche." :
              " Publiez un de vos agents pour le partager : la bascule est dans son éditeur."}
          </div></div>
        ) : (
          <div className="grid">
            {rows.map((r) => (
              <div className="card agent" key={r.id}>
                <h3>
                  {kind === "agent" && <span className="dot" style={{ background: accentOf(r.accent) }} />}
                  {r.name}
                </h3>
                <p>{(kind === "agent" ? r.description : r.objective) ||
                  <span className="muted">Pas de description.</span>}</p>
                <div className="foot">
                  <span className="tag">par {r.owner_label}</span>
                  {kind === "agent"
                    ? <span className="tag">{r.model}</span>
                    : <span className="tag">{r.team_size} agents</span>}
                  <div style={{ flex: 1 }} />
                  <button className="btn sm primary" disabled={copying === r.id}
                    onClick={() => duplicate(r)}>
                    {copying === r.id ? "copie…" : "Dupliquer"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
