import { useEffect, useState } from "react";
import { Agents } from "./Agents";
import { Projects } from "./Projects";
import { ProjectView } from "./ProjectView";
import { api } from "./api";

type View = { name: "agents" } | { name: "projects" } | { name: "project"; id: string };

export default function App() {
  const [view, setView] = useState<View>({ name: "projects" });
  const [status, setStatus] = useState<{ db: boolean; db_detail: string; live: boolean } | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => { api.state().then(setStatus).catch(() => setStatus({ db: false, db_detail: "API injoignable", live: false })); }, []);

  const seed = async () => {
    setSeeding(true);
    try {
      const r = await api.seed();
      setNonce((n) => n + 1);
      if (r.project_id) setView({ name: "project", id: r.project_id });
      else setView({ name: "agents" });
    } finally { setSeeding(false); }
  };

  return (
    <div className="app">
      <nav className="side">
        <div className="brand">
          <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
            <circle cx="16" cy="8" r="4.4" fill="var(--accent)" />
            <circle cx="7" cy="24" r="4.4" fill="var(--ink)" />
            <circle cx="25" cy="24" r="4.4" fill="var(--ink)" />
            <path d="M16 12.4 8.6 20M16 12.4 23.4 20" stroke="var(--line)" strokeWidth="1.6" />
          </svg>
          <b>Agent Studio<span>équipes d'agents IA</span></b>
        </div>

        <button className={"navbtn" + (view.name !== "agents" ? " on" : "")} onClick={() => setView({ name: "projects" })}>
          <span className="lbl-txt">Projets</span><span>◻</span>
        </button>
        <button className={"navbtn" + (view.name === "agents" ? " on" : "")} onClick={() => setView({ name: "agents" })}>
          <span className="lbl-txt">Agents</span><span>◇</span>
        </button>

        <div className="side-foot small muted">
          <div className="row" style={{ gap: 6 }}>
            <span className="dot" style={{ background: status?.db ? "var(--ok)" : "var(--stop)" }} />
            <span className="lbl-txt">{status?.db ? "base connectée" : "base injoignable"}</span>
          </div>
          <div className="row" style={{ gap: 6, marginTop: 6 }}>
            <span className="dot" style={{ background: status?.live ? "var(--ok)" : "var(--warn)" }} />
            <span className="lbl-txt">{status?.live ? "modèles actifs" : "mode démo"}</span>
          </div>
          <button className="btn sm ghost lbl-txt" style={{ marginTop: 10, paddingLeft: 0 }} disabled={seeding} onClick={seed}>
            {seeding ? "amorçage…" : "Amorcer une équipe"}
          </button>
        </div>
      </nav>

      <main className="main" key={nonce}>
        {view.name === "agents" && <Agents onChanged={() => setNonce((n) => n + 1)} />}
        {view.name === "projects" && <Projects onOpen={(id) => setView({ name: "project", id })} />}
        {view.name === "project" && (
          <ProjectView id={view.id} live={!!status?.live} onBack={() => setView({ name: "projects" })} />
        )}
      </main>
    </div>
  );
}
