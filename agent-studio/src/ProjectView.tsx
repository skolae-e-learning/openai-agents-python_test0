import { useCallback, useEffect, useRef, useState } from "react";
import { Canvas } from "./Canvas";
import { capLabel } from "./Connectors";
import { accentOf, api, type Agent, type Artifact, type Connector, type Cycle, type CycleImage, type Decision, type Edge, type Message, type Project, type TeamMember } from "./api";

type Tab = "journal" | "artifacts" | "images";

export function ProjectView({ id, live, onBack }: { id: string; live: boolean; onBack: () => void }) {
  const [project, setProject] = useState<Project | null>(null);
  const [team, setTeam] = useState<TeamMember[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [cycle, setCycle] = useState<Cycle | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [suggestions, setSuggestions] = useState<{ agent: Agent; reason: string }[]>([]);
  const [tab, setTab] = useState<Tab>("journal");
  const [brief, setBrief] = useState("");
  const [starting, setStarting] = useState(false);
  const [intervening, setIntervening] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [adding, setAdding] = useState(false);
  const [allAgents, setAllAgents] = useState<Agent[]>([]);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [allFolded, setAllFolded] = useState(false);
  // Connexions écartées, par décision : la carte rend toutes les décisions en attente.
  const [declined, setDeclined] = useState<Record<string, Set<string>>>({});
  const [images, setImages] = useState<CycleImage[]>([]);
  // Images choisies mais pas encore envoyées : au lancement, le cycle n'existe pas encore.
  const [queued, setQueued] = useState<{ file: File; caption: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [imageError, setImageError] = useState("");
  const after = useRef(0);
  const scroller = useRef<HTMLDivElement>(null);
  const inbox = useRef<HTMLDivElement>(null);

  const reload = useCallback(async () => {
    const d = await api.project(id);
    setProject(d.project); setTeam(d.team); setEdges(d.edges); setCycle(d.cycle);
    api.suggestions(id).then(setSuggestions).catch(() => {});
  }, [id]);

  useEffect(() => { after.current = 0; setMessages([]); reload(); }, [id, reload]);

  // Boucle vivante : on avance l'orchestration d'un step, puis on relit le flux.
  useEffect(() => {
    let alive = true;
    const loop = async () => {
      while (alive) {
        try {
          const s = await api.stream(id, after.current);
          if (!alive) return;
          if (s.messages.length) {
            after.current = s.messages[s.messages.length - 1].id;
            setMessages((m) => [...m, ...s.messages]);
          }
          setCycle(s.cycle); setProject(s.project);
          setDecisions(s.decisions); setArtifacts(s.artifacts);
          const running = s.project.state === "RUNNING" && s.cycle &&
            (s.cycle.status === "running" || s.cycle.status === "queued");
          if (running) await api.tick().catch(() => {});
          await new Promise((r) => setTimeout(r, running ? 350 : 2200));
        } catch {
          await new Promise((r) => setTimeout(r, 3000));
        }
      }
    };
    loop();
    return () => { alive = false; };
  }, [id]);

  useEffect(() => {
    const el = scroller.current;
    if (el && tab === "journal") el.scrollTop = el.scrollHeight;
  }, [messages.length, tab]);

  const cycleId = cycle?.id;
  useEffect(() => {
    if (!cycleId) { setImages([]); return; }
    api.cycleImages(cycleId).then(setImages).catch(() => {});
  }, [cycleId]);

  if (!project) return <div className="body"><div className="empty">Chargement…</div></div>;

  const state = project.state;
  const active = state === "RUNNING";
  const paused = state === "PAUSED";
  const waiting = cycle?.status === "waiting_human";
  const progress = cycle?.plan?.length ? `step ${Math.min(cycle.cursor + 1, cycle.plan.length)} / ${cycle.plan.length}` : "";

  // Le bandeau doit dire pourquoi c'est bloqué sans qu'on ouvre le journal.
  const pending = decisions[0];
  const reason = (pending?.payload as any)?.reason;
  const blockReason = waiting
    ? (reason
        ? `${reason.agent} demande votre accord avant « ${reason.action} ». ${reason.why}`
        : pending?.detail || "Une action attend votre validation avant de se poursuivre.")
    : paused
      ? "Vous avez mis le cycle en pause. Le step en cours s'est terminé, aucun nouveau step n'est planifié. L'état est conservé : la reprise repartira exactement d'ici."
      : cycle?.status === "failed"
        ? "Un step a échoué. Le détail exact figure dans le journal, au dernier message système."
        : cycle?.note || "Le cycle a été arrêté. Les contenus déjà produits sont conservés.";

  // Les images déposées avant le lancement attendent l'identifiant du cycle,
  // qui n'existe qu'une fois celui-ci créé.
  const uploadTo = async (target: string, items: { file: File; caption: string }[]) => {
    const sent: CycleImage[] = [];
    for (const item of items) {
      try {
        sent.push(await api.addCycleImage(target, item.file, item.caption));
      } catch (e: any) {
        setImageError(`${item.file.name} — ${e?.message || "envoi impossible"}`);
      }
    }
    return sent;
  };

  const start = async () => {
    setStarting(false);
    setUploading(true);
    after.current = 0; setMessages([]);
    try {
      const created = await api.startCycle(id, brief);
      if (queued.length) {
        const sent = await uploadTo(created.id, queued);
        setImages(sent);
        setQueued([]);
      }
      setBrief("");
    } finally {
      setUploading(false);
      reload();
    }
  };

  const attachNow = async (files: FileList | null) => {
    if (!files?.length || !cycle) return;
    setImageError("");
    setUploading(true);
    try {
      await uploadTo(cycle.id, Array.from(files).map((file) => ({ file, caption: "" })));
      setImages(await api.cycleImages(cycle.id));
    } finally {
      setUploading(false);
    }
  };

  const dropImage = async (imageId: string) => {
    await api.deleteImage(imageId);
    if (cycle) setImages(await api.cycleImages(cycle.id));
  };

  // Ce qui reste couvert si l'humain écarte une connexion, et ce qui ne l'est plus.
  const arbitration = (d: Decision) => {
    const offered: Connector[] = d.payload?.connectors || [];
    const off = declined[d.id] || new Set<string>();
    const kept = offered.filter((c) => !off.has(c.id));
    const keptCaps = new Set(kept.flatMap((c) => c.capabilities));
    const lost = offered
      .filter((c) => off.has(c.id))
      .flatMap((c) => c.capabilities)
      .filter((cap) => !keptCaps.has(cap));
    return { offered, off, kept, lost: Array.from(new Set(lost)) };
  };

  const toggleConnector = (decisionId: string, connectorId: string) =>
    setDeclined((prev) => {
      const next = new Set(prev[decisionId] || []);
      if (next.has(connectorId)) next.delete(connectorId);
      else next.add(connectorId);
      return { ...prev, [decisionId]: next };
    });

  const control = async (action: string, text = "") => { await api.control(id, action, text); reload(); };

  const moveNode = async (agentId: string, x: number, y: number) => {
    const next = team.map((t) => (t.id === agentId ? { ...t, x, y } : t));
    setTeam(next);
    await api.setTeam(id, next.map((t) => ({ agent_id: t.id, role: t.team_role, x: t.x, y: t.y })));
  };

  const toggleEdge = async (source: string, target: string) => {
    const exists = edges.some((e) => e.source_agent_id === source && e.target_agent_id === target);
    const kept = exists
      ? edges.filter((e) => !(e.source_agent_id === source && e.target_agent_id === target))
      : [...edges, { id: "tmp", source_agent_id: source, target_agent_id: target,
          kind: team.find((t) => t.id === target)?.team_role === "critic" ? "reviews" : "calls" } as Edge];
    setEdges(kept);
    await api.setEdges(id, kept.map((e) => ({ source: e.source_agent_id, target: e.target_agent_id, kind: e.kind })));
  };

  const toggleMsg = (msgId: number) => {
    setCollapsed((s) => {
      const next = new Set(s);
      next.has(msgId) ? next.delete(msgId) : next.add(msgId);
      return next;
    });
  };

  const foldAll = () => {
    if (allFolded) { setCollapsed(new Set()); setAllFolded(false); }
    else { setCollapsed(new Set(messages.map((m) => m.id))); setAllFolded(true); }
  };

  const openAdd = async () => {
    setAllAgents(await api.agents());
    setAdding(true);
  };

  const addSuggested = async (a: Agent) => {
    const role = a.role === "orchestrator" ? "orchestrator" : a.role === "critic" ? "critic" : "specialist";
    const next = [...team.map((t) => ({ agent_id: t.id, role: t.team_role, x: t.x, y: t.y })),
                  { agent_id: a.id, role, x: 660, y: 340 }];
    await api.setTeam(id, next);
    await reload();
  };

  return (
    <>
      <div className="head">
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 9 }}>
            <button className="btn sm ghost" onClick={onBack}>← Projets</button>
            <span className={"tag " + (active ? "ok" : paused ? "warn" : waiting ? "warn" : "")}>
              {waiting ? "en attente d'arbitrage" : active ? "en cours" : paused ? "en pause" : "au repos"}
            </span>
            {progress && <span className="tag">{progress}</span>}
            {cycle?.demo && <span className="tag">mode démo</span>}
            <button className={"tag " + (project.visibility === "public" ? "on" : "")}
              style={{ cursor: "pointer", background: "none" }}
              title="Un projet public est visible dans Explorer et duplicable par d'autres comptes."
              onClick={async () => {
                await api.updateProject(id, {
                  name: project.name, objective: project.objective,
                  max_revisions: project.max_revisions,
                  visibility: project.visibility === "public" ? "private" : "public",
                });
                reload();
              }}>
              {project.visibility === "public" ? "public" : "privé"}
            </button>
          </div>
          <h1 style={{ marginTop: 7 }}>{project.name}</h1>
          <p>{project.objective}</p>
        </div>
        <div className="spacer" />
        <div className="row" style={{ justifyContent: "flex-end" }}>
          {!active && !paused && <button className="btn primary" onClick={() => setStarting(true)}>Lancer un cycle</button>}
          {active && <button className="btn" onClick={() => control("pause")}>⏸ Pause</button>}
          {paused && <button className="btn primary" onClick={() => control("resume")}>▶ Reprendre</button>}
          {(active || paused || waiting) && <button className="btn" onClick={() => control("stop")}>⛔ Arrêter</button>}
          <button className="btn" onClick={() => setIntervening(true)}>👤 Intervenir</button>
        </div>
      </div>

      <div className="body">
        {(waiting || paused || cycle?.status === "failed" || cycle?.status === "stopped") && (
          <div className={"alert" + (cycle?.status === "failed" || cycle?.status === "stopped" ? " stopped" : "")}>
            <span className="ic">{waiting ? "⏸" : paused ? "⏸" : "⛔"}</span>
            <div style={{ flex: 1, minWidth: 220 }}>
              <h4>
                {waiting ? "Cycle suspendu — votre arbitrage est nécessaire"
                  : paused ? "Cycle en pause"
                  : cycle?.status === "failed" ? "Cycle interrompu par une erreur"
                  : "Cycle arrêté"}
              </h4>
              <p>{blockReason}</p>
              {cycle?.plan?.length ? (
                <div className="meta">
                  <span className="tag">step {Math.min(cycle.cursor + 1, cycle.plan.length)} / {cycle.plan.length}</span>
                  <span className="tag">{artifacts.length} production{artifacts.length > 1 ? "s" : ""} conservée{artifacts.length > 1 ? "s" : ""}</span>
                </div>
              ) : null}
              {/* Propre ligne : les connexions en jeu doivent se lire même sans plan. */}
              {waiting && (pending?.payload?.connectors?.length || pending?.payload?.missing?.length) ? (
                <div className="meta">
                  {(pending.payload?.connectors || []).map((c) => (
                    <span className={"tag " + (declined[pending.id]?.has(c.id) ? "" : "on")} key={c.id}>
                      {declined[pending.id]?.has(c.id) ? "écarté : " : ""}{c.name}
                    </span>
                  ))}
                  {(pending.payload?.missing || []).map((cap) => (
                    <span className="tag warn" key={cap}>sans connexion : {capLabel(cap)}</span>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="row">
              {waiting && decisions.length > 0 && (
                <button className="btn primary" onClick={() => inbox.current?.scrollIntoView({ behavior: "smooth", block: "center" })}>
                  Arbitrer maintenant
                </button>
              )}
              {paused && <button className="btn primary" onClick={() => control("resume")}>▶ Reprendre</button>}
            </div>
          </div>
        )}

        {!live && (
          <div className="banner warn" style={{ marginBottom: 16 }}>
            <b>Mode démo.</b>
            <span>Aucune clé <code>OPENAI_API_KEY</code> n'est configurée sur ce déploiement. Le parcours, l'écriture en base, la boucle de révision et la suspension pour validation sont réels ; seul le texte produit par les agents est fabriqué localement. Ajoutez la variable d'environnement pour passer en exécution réelle.</span>
          </div>
        )}

        <div className="cols">
          <div style={{ display: "grid", gap: 18 }}>
            <div className="card">
              <div className="card-h">
                Architecture — qui parle à qui
                <div style={{ flex: 1 }} />
                <button className="btn sm" onClick={openAdd}>+ Ajouter un agent</button>
              </div>
              <div className="card-b">
                <Canvas team={team} edges={edges} onMove={moveNode} onToggleEdge={toggleEdge} />
              </div>
            </div>

            {suggestions.length > 0 && (
              <div className="card">
                <div className="card-h">L'application propose</div>
                <div className="card-b" style={{ display: "grid", gap: 10 }}>
                  <div className="small muted">Ces agents existants recoupent l'objectif du projet. L'application propose, elle ne modifie jamais l'équipe toute seule.</div>
                  {suggestions.map((s) => (
                    <div key={s.agent.id} className="row">
                      <span className="dot" style={{ background: accentOf(s.agent.accent) }} />
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <b style={{ fontWeight: 600 }}>{s.agent.name}</b>
                        <span className="small muted" style={{ display: "block" }}>{s.reason}</span>
                      </span>
                      <button className="btn sm" onClick={() => addSuggested(s.agent)}>Ajouter au projet</button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <div style={{ display: "grid", gap: 18 }}>
            {decisions.length > 0 && (
              <div className="card">
                <div className="card-h">À arbitrer · {decisions.length}</div>
                <div className="card-b">
                  {decisions.map((d) => (
                    <div className="decision" key={d.id} ref={d.id === decisions[0].id ? inbox : undefined}>
                      <h4>{d.title}</h4>
                      <div className="small" style={{ color: "var(--ink-2)" }}>{d.detail}</div>
                      {d.payload?.reason?.why && (
                        <div className="small" style={{ color: "var(--ink-2)", marginTop: 6 }}>
                          <b>Pourquoi c'est bloqué :</b> {d.payload.reason.why}
                        </div>
                      )}
                      {(() => {
                        const { offered, off, kept, lost } = arbitration(d);
                        const missing = d.payload?.missing || [];
                        if (!offered.length && !missing.length) return null;
                        return (
                          <div style={{ marginTop: 10 }}>
                            {offered.length > 0 && (
                              <>
                                <div className="small" style={{ color: "var(--ink-2)", marginBottom: 4 }}>
                                  <b>Connexions mobilisées si vous autorisez.</b> Décochez celles
                                  qui ne doivent pas agir.
                                </div>
                                {offered.map((c) => (
                                  <label key={c.id} className="row"
                                    style={{ padding: "5px 0", cursor: "pointer", alignItems: "flex-start" }}>
                                    <input type="checkbox" checked={!off.has(c.id)} style={{ marginTop: 3 }}
                                      onChange={() => toggleConnector(d.id, c.id)} />
                                    <span style={{ flex: 1, minWidth: 0 }}>
                                      <b style={{ fontWeight: 600 }}>{c.name}</b>
                                      <span className="small muted" style={{ display: "block" }}>
                                        {c.label} · {c.capabilities.map(capLabel).join(", ")}
                                      </span>
                                    </span>
                                    <span className={"tag " + (c.status === "ok" ? "ok" : c.status === "error" ? "stop" : "warn")}>
                                      {c.status === "ok" ? "connectée" : c.status === "error" ? "en échec" : "à tester"}
                                    </span>
                                  </label>
                                ))}
                              </>
                            )}
                            {lost.map((cap) => {
                              const alt = kept.find((c) => c.capabilities.includes(cap));
                              return (
                                <div className="small" key={cap}
                                  style={{ color: "var(--ink-2)", marginTop: 4 }}>
                                  {alt
                                    ? <>Le volet « {capLabel(cap)} » reste couvert par « {alt.name} ».</>
                                    : <>Plus aucune connexion ne couvre « {capLabel(cap)} » : cette diffusion sera ignorée.</>}
                                </div>
                              );
                            })}
                            {missing.length > 0 && (
                              <div className="small muted" style={{ marginTop: 6 }}>
                                Sans connexion déclarée : {missing.map(capLabel).join(", ")}. Ces
                                diffusions ne partiront pas. Ajoutez-les dans l'écran Connecteurs.
                              </div>
                            )}
                          </div>
                        );
                      })()}
                      <div className="row" style={{ marginTop: 10 }}>
                        <button className="btn sm primary" onClick={async () => {
                          await api.respond(d.id, "approved", "", arbitration(d).kept.map((c) => c.id));
                          reload();
                        }}>Autoriser</button>
                        <button className="btn sm" onClick={async () => { await api.respond(d.id, "rejected"); reload(); }}>Rejeter</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="card">
              <div className="card-h" style={{ gap: 4 }}>
                <button className={"btn sm ghost" + (tab === "journal" ? " " : "")} style={{ color: tab === "journal" ? "var(--accent)" : undefined }} onClick={() => setTab("journal")}>Journal</button>
                <button className="btn sm ghost" style={{ color: tab === "artifacts" ? "var(--accent)" : undefined }} onClick={() => setTab("artifacts")}>Productions · {artifacts.length}</button>
                <button className="btn sm ghost" style={{ color: tab === "images" ? "var(--accent)" : undefined }} onClick={() => setTab("images")}>Images · {images.length}</button>
                <div style={{ flex: 1 }} />
                {tab === "journal" && messages.length > 0 && (
                  <button className="btn sm ghost" onClick={foldAll}>
                    {allFolded ? "Tout déplier" : "Tout replier"}
                  </button>
                )}
              </div>
              {tab === "journal" ? (
                <div className="journal" ref={scroller}>
                  {messages.length === 0
                    ? <div className="empty">Le journal est vide. Lancez un cycle pour voir les agents travailler.</div>
                    : messages.map((m) => {
                      const long = (m.body || "").length > 200;
                      const shut = collapsed.has(m.id);
                      return (
                        <div key={m.id} className={`msg d${Math.min(m.depth, 2)} k-${m.kind}`}>
                          {long ? (
                            <button className="head-row" onClick={() => toggleMsg(m.id)}
                              aria-expanded={!shut}>
                              <span className={"chev" + (shut ? "" : " open")}>▶</span>
                              <span className="t" style={{ flex: 1 }}>
                                {m.agent_name && <span className="dot" style={{ background: accentOf(team.find((t) => t.name === m.agent_name)?.accent) }} />}
                                {m.title}
                              </span>
                            </button>
                          ) : (
                            <div className="t">
                              {m.agent_name && <span className="dot" style={{ background: accentOf(team.find((t) => t.name === m.agent_name)?.accent) }} />}
                              {m.title}
                            </div>
                          )}
                          {m.body && <div className={"b" + (long && shut ? " clamped" : "")}>{m.body}</div>}
                        </div>
                      );
                    })}
                </div>
              ) : tab === "images" ? (
                <div className="journal">
                  <div className="art" style={{ paddingBottom: 12 }}>
                    <label className="btn sm" style={{ display: "inline-block" }}>
                      {uploading ? "Envoi…" : "Joindre une image"}
                      <input type="file" accept="image/*" multiple hidden disabled={!cycle || uploading}
                        onChange={(e) => { attachNow(e.target.files); e.target.value = ""; }} />
                    </label>
                    <span className="small muted" style={{ marginLeft: 9 }}>
                      {cycle
                        ? `3 Mo par image, 8 au maximum. Les agents les reçoivent au step suivant.`
                        : "Lancez un cycle pour pouvoir y joindre des images."}
                    </span>
                    {imageError && (
                      <div className="banner warn" style={{ marginTop: 9 }}>{imageError}</div>
                    )}
                  </div>
                  {images.length === 0
                    ? <div className="empty">Aucune image jointe à ce cycle.</div>
                    : images.map((img) => (
                      <div className="art" key={img.id}>
                        <div className="art-meta">
                          <h4>image</h4>
                          <span className="who">{img.filename}</span>
                        </div>
                        <img className="art-img" src={img.url} alt={img.caption || img.filename} />
                        {img.caption && <div className="small muted" style={{ marginTop: 6 }}>{img.caption}</div>}
                        <div className="art-actions">
                          <button className="btn sm ghost danger" onClick={() => dropImage(img.id)}>
                            Supprimer
                          </button>
                        </div>
                      </div>
                    ))}
                </div>
              ) : (
                <div className="journal">
                  {artifacts.length === 0
                    ? <div className="empty">Aucune production pour l'instant.</div>
                    : artifacts.map((a) => (
                      <div className="art" key={a.id}>
                        <div className="art-meta">
                          <h4>{a.type}</h4>
                          <span className="who">
                            <span className="dot" style={{ background: accentOf(team.find((t) => t.name === a.agent_name)?.accent), marginRight: 5 }} />
                            {a.agent_name}
                          </span>
                          <span className="who">· {a.title}</span>
                        </div>
                        <div className="art-body">{a.body}</div>
                        <div className="art-actions">
                          <button className="btn sm ghost"
                            onClick={() => navigator.clipboard?.writeText(a.body)}>
                            Copier la production
                          </button>
                        </div>
                      </div>
                    ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {starting && (
        <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && setStarting(false)}>
          <div className="modal">
            <div className="modal-h">Lancer un cycle</div>
            <div className="modal-b">
              <label className="f">
                <span>Consigne du cycle — laissez vide pour reprendre l'objectif permanent du projet</span>
                <textarea className="f" rows={3} value={brief} autoFocus
                  placeholder="Un post qui explique pourquoi l'empilement d'outils ne règle pas le problème."
                  onChange={(e) => setBrief(e.target.value)} />
              </label>
              <label className="f">
                <span>Images à joindre — les agents les recevront avec la consigne</span>
                <span className="row">
                  <label className="btn sm" style={{ display: "inline-block" }}>
                    Choisir des images
                    <input type="file" accept="image/*" multiple hidden
                      onChange={(e) => {
                        const picked = Array.from(e.target.files || []).map((file) => ({ file, caption: "" }));
                        setQueued((q) => [...q, ...picked].slice(0, 8));
                        e.target.value = "";
                      }} />
                  </label>
                  {queued.length > 0 && (
                    <span className="small muted">{queued.length} image(s) en attente</span>
                  )}
                </span>
              </label>
              {queued.map((item, i) => (
                <div className="row small" key={i}
                  style={{ padding: "4px 0", borderBottom: "1px solid var(--line-soft)" }}>
                  <span style={{ flex: 1, minWidth: 0 }}>{item.file.name}</span>
                  <input className="f" style={{ flex: 1, minWidth: 120 }} placeholder="Légende…"
                    value={item.caption}
                    onChange={(e) =>
                      setQueued((q) => q.map((x, j) => (j === i ? { ...x, caption: e.target.value } : x)))} />
                  <button className="btn sm ghost"
                    onClick={() => setQueued((q) => q.filter((_, j) => j !== i))}>×</button>
                </div>
              ))}

              <div className="small muted" style={{ marginTop: 11 }}>
                Le chef d'orchestre établira le plan, les spécialistes produiront, le critique évaluera et pourra renvoyer en révision, puis la publication demandera votre validation.
              </div>
            </div>
            <div className="modal-f">
              <button className="btn ghost" onClick={() => setStarting(false)}>Annuler</button>
              <button className="btn primary" disabled={uploading} onClick={start}>Lancer</button>
            </div>
          </div>
        </div>
      )}

      {adding && (
        <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && setAdding(false)}>
          <div className="modal">
            <div className="modal-h">Ajouter un agent au projet</div>
            <div className="modal-b">
              {allAgents.filter((a) => !team.some((t) => t.id === a.id)).length === 0 ? (
                <div className="empty">Tous vos agents font déjà partie de cette équipe.</div>
              ) : (
                allAgents.filter((a) => !team.some((t) => t.id === a.id)).map((a) => (
                  <div key={a.id} className="row" style={{ padding: "8px 0", borderBottom: "1px solid var(--line-soft)" }}>
                    <span className="dot" style={{ background: accentOf(a.accent) }} />
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <b style={{ fontWeight: 600 }}>{a.name}</b>
                      <span className="small muted" style={{ display: "block" }}>{a.description}</span>
                    </span>
                    <span className="tag">{a.role === "orchestrator" ? "chef" : a.role === "critic" ? "critique" : "spécialiste"}</span>
                    <button className="btn sm primary"
                      onClick={async () => { await addSuggested(a); setAdding(false); }}>Ajouter</button>
                  </div>
                ))
              )}
            </div>
            <div className="modal-f">
              <button className="btn ghost" onClick={() => setAdding(false)}>Fermer</button>
            </div>
          </div>
        </div>
      )}

      {intervening && (
        <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && setIntervening(false)}>
          <div className="modal">
            <div className="modal-h">Intervenir dans la réflexion</div>
            <div className="modal-b">
              <label className="f">
                <span>Votre instruction. Elle est injectée dans le cycle et s'applique aux steps restants.</span>
                <textarea className="f" rows={3} value={instruction} autoFocus
                  placeholder="Ne cible plus les PME. À partir de maintenant, cible les agences."
                  onChange={(e) => setInstruction(e.target.value)} />
              </label>
            </div>
            <div className="modal-f">
              <button className="btn ghost" onClick={() => setIntervening(false)}>Annuler</button>
              <button className="btn primary" disabled={!instruction.trim()}
                onClick={async () => { await control("intervene", instruction); setInstruction(""); setIntervening(false); }}>
                Injecter
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
