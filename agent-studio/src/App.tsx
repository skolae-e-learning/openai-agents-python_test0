import { useEffect, useState } from "react";
import { Agents } from "./Agents";
import { Connectors } from "./Connectors";
import { Explore } from "./Explore";
import { Login } from "./Login";
import { Projects } from "./Projects";
import { ProjectView } from "./ProjectView";
import * as auth from "./auth";
import { api, type Summary } from "./api";

type View =
  | { name: "agents" }
  | { name: "projects" }
  | { name: "explore" }
  | { name: "connectors" }
  | { name: "project"; id: string };

type Status = { db: boolean; db_detail: string; live: boolean; auth: auth.AuthConfig };

export default function App() {
  const [view, setView] = useState<View>({ name: "projects" });
  const [status, setStatus] = useState<Status | null>(null);
  const [me, setMe] = useState<auth.Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [seeding, setSeeding] = useState(false);
  const [nonce, setNonce] = useState(0);
  const [agentBoot, setAgentBoot] = useState(0);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [installer, setInstaller] = useState<any>(null);
  const [legacy, setLegacy] = useState<{ agents: number; projects: number } | null>(null);

  const goCreateAgent = () => { setView({ name: "agents" }); setAgentBoot((n) => n + 1); };
  const bump = () => setNonce((n) => n + 1);

  // Un seul appel décide de tout : état de la base, mode réel, configuration Auth.
  useEffect(() => {
    api.state()
      .then(async (s) => {
        setStatus(s);
        if (s.auth?.ready) {
          auth.setConfig(s.auth);
          setMe(await auth.me());
        }
      })
      .catch(() => setStatus({
        db: false, db_detail: "API injoignable", live: false,
        auth: { ready: false, project_id: "", publishable_key: "" },
      }))
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    if (!me) { setSummary(null); setLegacy(null); return; }
    let alive = true;
    const pull = () => {
      api.summary().then((s) => { if (alive) setSummary(s); }).catch(() => {});
      api.legacy().then((l) => { if (alive) setLegacy(l); }).catch(() => {});
    };
    pull();
    const t = setInterval(pull, 10000);
    return () => { alive = false; clearInterval(t); };
  }, [me, nonce]);

  // Chrome n'expose l'installation qu'à travers cet évènement : on le capture pour
  // proposer le bouton au bon moment plutôt qu'un bouton toujours inerte.
  useEffect(() => {
    const onPrompt = (e: Event) => { e.preventDefault(); setInstaller(e); };
    window.addEventListener("beforeinstallprompt", onPrompt);
    window.addEventListener("appinstalled", () => setInstaller(null));
    return () => window.removeEventListener("beforeinstallprompt", onPrompt);
  }, []);

  const seed = async () => {
    setSeeding(true);
    try {
      const r = await api.seed();
      bump();
      if (r.project_id) setView({ name: "project", id: r.project_id });
      else setView({ name: "agents" });
    } finally { setSeeding(false); }
  };

  const claim = async () => {
    await api.claimLegacy();
    setLegacy({ agents: 0, projects: 0 });
    bump();
  };

  const leave = async () => {
    await auth.signOut();
    setMe(null);
    setView({ name: "projects" });
  };

  if (checking) return <div className="login-wrap"><div className="empty">Chargement…</div></div>;
  if (status && !status.db) return <div className="app"><main className="main"><Setup detail={status.db_detail} /></main></div>;
  if (status && !status.auth?.ready) {
    return <div className="app"><main className="main"><AuthMissing /></main></div>;
  }
  if (!me) return <Login onSignedIn={setMe} />;

  // Le glyphe est hors de `lbl-txt` : replié, le rail doit rester compréhensible.
  const nav = (target: View, label: string, icon: string, extra?: React.ReactNode) => (
    <button className={"navbtn" + (view.name === target.name ? " on" : "")}
      title={label} aria-label={label} onClick={() => setView(target)}>
      <span className="navico" aria-hidden>{icon}</span>
      <span className="lbl-txt">{label}</span>
      {extra}
    </button>
  );

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

        {nav({ name: "projects" }, "Projets", "▤", (
          <span className="counts lbl-txt">
            {!!summary?.running && <span className="ct ok" title={`${summary.running} projet(s) en cours`}>▶ {summary.running}</span>}
            {!!summary?.paused && <span className="ct warn" title={`${summary.paused} projet(s) en pause`}>⏸ {summary.paused}</span>}
          </span>
        ))}
        {nav({ name: "agents" }, "Agents", "◇")}
        {nav({ name: "explore" }, "Explorer", "◎")}
        {nav({ name: "connectors" }, "Connecteurs", "⇄")}

        <div className="side-foot small muted">
          <div className="who lbl-txt" title={me.email}>{me.email}</div>
          <button className="btn sm ghost" style={{ paddingLeft: 0 }} title={me.email} onClick={leave}>
            Se déconnecter
          </button>
          <div className="row" style={{ gap: 6, marginTop: 10 }}
            title={status?.db ? "base connectée" : "base injoignable"}>
            <span className="dot" style={{ background: status?.db ? "var(--ok)" : "var(--stop)" }} />
            <span className="lbl-txt">{status?.db ? "base connectée" : "base injoignable"}</span>
          </div>
          <div className="row" style={{ gap: 6, marginTop: 6 }}
            title={status?.live ? "modèles actifs" : "mode démo"}>
            <span className="dot" style={{ background: status?.live ? "var(--ok)" : "var(--warn)" }} />
            <span className="lbl-txt">{status?.live ? "modèles actifs" : "mode démo"}</span>
          </div>
          {installer && (
            <button className="btn sm" style={{ marginTop: 10 }} title="Installer l'application"
              onClick={async () => { installer.prompt(); await installer.userChoice; setInstaller(null); }}>
              Installer l'application
            </button>
          )}
        </div>
      </nav>

      <main className="main" key={nonce}>
        {!!legacy && legacy.agents + legacy.projects > 0 && view.name !== "project" && (
          <div style={{ padding: "16px 26px 0" }}>
            <div className="banner info" style={{ alignItems: "center" }}>
              <span style={{ flex: 1 }}>
                {legacy.agents} agent{legacy.agents > 1 ? "s" : ""} et {legacy.projects} projet
                {legacy.projects > 1 ? "s" : ""} datent d'avant la mise en place des comptes et
                n'appartiennent à personne. Vous pouvez les rattacher au vôtre.
              </span>
              <button className="btn sm primary" onClick={claim}>Les récupérer</button>
            </div>
          </div>
        )}

        {view.name === "agents" && (
          <Agents onChanged={bump} autoOpenCreate={agentBoot} onLoadDemo={seed} seeding={seeding} />
        )}
        {view.name === "projects" && (
          <Projects onOpen={(id) => setView({ name: "project", id })} onCreateAgent={goCreateAgent}
            onLoadDemo={seed} seeding={seeding} />
        )}
        {view.name === "explore" && (
          <Explore onOpenProject={(id) => setView({ name: "project", id })} onChanged={bump} />
        )}
        {view.name === "connectors" && <Connectors />}
        {view.name === "project" && (
          <ProjectView id={view.id} live={!!status?.live} onBack={() => setView({ name: "projects" })} />
        )}
      </main>
    </div>
  );
}

function AuthMissing() {
  return (
    <>
      <div className="head">
        <div>
          <h1>Authentification non configurée</h1>
          <p>L'application est déployée mais Neon Auth n'est pas joignable.</p>
        </div>
      </div>
      <div className="body">
        <div className="card" style={{ maxWidth: 720 }}>
          <div className="card-b">
            <p style={{ margin: 0, color: "var(--ink-2)" }}>
              Les variables <code>STACK_PROJECT_ID</code> et <code>STACK_PUBLISHABLE_CLIENT_KEY</code>
              {" "}doivent être définies sur le projet Vercel. Elles proviennent de Neon Auth et sont
              publiques par conception.
            </p>
          </div>
        </div>
      </div>
    </>
  );
}

function Setup({ detail }: { detail: string }) {
  return (
    <>
      <div className="head">
        <div>
          <h1>Configuration requise</h1>
          <p>L'application est déployée, mais elle n'a pas encore accès à sa base de données.</p>
        </div>
      </div>
      <div className="body">
        <div className="card" style={{ maxWidth: 760 }}>
          <div className="card-h">Une variable d'environnement à ajouter</div>
          <div className="card-b" style={{ display: "grid", gap: 14 }}>
            <p style={{ margin: 0, color: "var(--ink-2)" }}>
              Dans les réglages du projet Vercel, section <b>Environment Variables</b>, ajoutez une
              variable nommée <code>DATABASE_URL</code> dont la valeur est la chaîne de connexion du
              projet Neon nommé <b>agent-studio</b>.
            </p>
            <div className="banner warn" style={{ display: "block" }}>
              <b>Réponse de l'API :</b> {detail}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
