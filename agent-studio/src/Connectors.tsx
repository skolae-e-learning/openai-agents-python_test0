import { useEffect, useState } from "react";

import { api, type Connector, type ConnectorKind } from "./api";

type Draft = {
  id?: string;
  kind: string;
  name: string;
  config: Record<string, string>;
  secret: string;
};

// Libellés courts des capacités. Une capacité sans libellé s'affiche telle quelle
// plutôt que de disparaître : mieux vaut un terme technique qu'un trou.
const CAP_LABELS: Record<string, string> = {
  "llm.text": "texte",
  "llm.image": "image",
  "audio.tts": "voix",
  "mail.send": "courriel",
  "workspace.write": "espace de travail",
  "repo.write": "dépôt de code",
  deploy: "déploiement",
  "http.generic": "HTTP",
};
export const capLabel = (c: string) => CAP_LABELS[c] || c;

const STATUS: Record<string, [string, string]> = {
  ok: ["ok", "connectée"],
  error: ["stop", "en échec"],
  untested: ["warn", "à tester"],
};

export function Connectors() {
  const [kinds, setKinds] = useState<ConnectorKind[]>([]);
  const [ready, setReady] = useState(true);
  const [rows, setRows] = useState<Connector[]>([]);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  const load = () =>
    api
      .connectors()
      .then(setRows)
      .catch((e: any) => setMessage(e?.message || "Chargement impossible."))
      .finally(() => setLoading(false));

  useEffect(() => {
    api.connectorKinds().then((k) => { setKinds(k.kinds); setReady(k.ready); }).catch(() => {});
    load();
  }, []);

  const def = (kind: string) => kinds.find((k) => k.kind === kind);

  const open = (c?: Connector) =>
    setDraft(
      c
        ? { id: c.id, kind: c.kind, name: c.name, config: { ...c.config }, secret: "" }
        : { kind: kinds[0]?.kind || "http", name: "", config: {}, secret: "" },
    );

  const save = async () => {
    if (!draft) return;
    setBusy(true);
    setMessage("");
    try {
      const body = { kind: draft.kind, name: draft.name, config: draft.config, secret: draft.secret };
      if (draft.id) await api.updateConnector(draft.id, body);
      else await api.createConnector(body);
      setDraft(null);
      await load();
    } catch (e: any) {
      setMessage(e?.message || "Enregistrement impossible.");
    } finally {
      setBusy(false);
    }
  };

  const test = async (c: Connector) => {
    setTesting(c.id);
    setMessage("");
    try {
      const updated = await api.testConnector(c.id);
      setRows((rs) => rs.map((r) => (r.id === updated.id ? updated : r)));
      setMessage(`${updated.name} — ${updated.status_detail}`);
    } catch (e: any) {
      setMessage(e?.message || "Le test a échoué.");
    } finally {
      setTesting(null);
    }
  };

  const remove = async (c: Connector) => {
    if (!confirm(`Supprimer la connexion « ${c.name} » ? Les cycles qui s'en servaient ne pourront plus.`)) return;
    await api.deleteConnector(c.id);
    await load();
  };

  const current = draft ? def(draft.kind) : undefined;

  return (
    <>
      <div className="head">
        <div>
          <h1>Connecteurs</h1>
          <p>
            Les services extérieurs que vos agents peuvent mobiliser : modèles, courriel, espaces
            de travail, dépôts de code, déploiement. Au moment de publier, le cycle s'arrête et
            vous choisissez lesquels agissent réellement.
          </p>
        </div>
        <div className="spacer" />
        <button className="btn primary" onClick={() => open()} disabled={!kinds.length}>
          Nouvelle connexion
        </button>
      </div>

      <div className="body">
        {!ready && (
          <div className="banner warn" style={{ marginBottom: 14 }}>
            La clé de chiffrement <code>APP_SECRET_KEY</code> n'est pas configurée sur ce
            déploiement. Tant qu'elle manque, aucun secret ne peut être enregistré — plutôt que
            de le stocker en clair.
          </div>
        )}
        {message && <div className="banner info" style={{ marginBottom: 14 }}>{message}</div>}

        {loading ? (
          <div className="card"><div className="empty">Chargement…</div></div>
        ) : rows.length === 0 ? (
          <div className="card"><div className="empty">
            Aucune connexion pour l'instant. Commencez par le connecteur HTTP générique : une URL
            suffit, et il couvre tout service acceptant un POST.
          </div></div>
        ) : (
          <div className="grid">
            {rows.map((c) => {
              const [tone, label] = STATUS[c.status] || STATUS.untested;
              return (
                <div className="card agent" key={c.id}>
                  <h3>{c.name}</h3>
                  <p className="muted">{c.label}</p>
                  <div className="foot">
                    <span className={"tag " + tone}>{label}</span>
                    {c.capabilities.map((cap) => (
                      <span className="tag" key={cap}>{capLabel(cap)}</span>
                    ))}
                  </div>
                  {c.status_detail && (
                    <p className="small muted" style={{ marginTop: 8 }}>{c.status_detail}</p>
                  )}
                  <div className="foot">
                    <button className="btn sm" disabled={testing === c.id} onClick={() => test(c)}>
                      {testing === c.id ? "Test en cours…" : "Tester"}
                    </button>
                    <button className="btn sm ghost" onClick={() => open(c)}>Modifier</button>
                    <div style={{ flex: 1 }} />
                    <button className="btn sm ghost danger" onClick={() => remove(c)}>Supprimer</button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {draft && current && (
        <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && setDraft(null)}>
          <div className="modal">
            <div className="modal-h">{draft.id ? "Modifier la connexion" : "Nouvelle connexion"}</div>
            <div className="modal-b">
              <label className="f">
                <span>Service</span>
                <select className="f" value={draft.kind}
                  onChange={(e) => setDraft({ ...draft, kind: e.target.value, config: {} })}>
                  {kinds.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
                </select>
              </label>
              {current.help && <div className="small muted" style={{ marginBottom: 11 }}>{current.help}</div>}

              <label className="f">
                <span>Nom — pour vous y retrouver dans l'arbitrage</span>
                <input className="f" value={draft.name} autoFocus placeholder="Courriel de l'équipe"
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
              </label>

              {current.fields.map((f) => (
                <label className="f" key={f.key}>
                  <span>{f.label}{f.required ? "" : " — facultatif"}</span>
                  <input className="f" value={draft.config[f.key] || ""} placeholder={f.placeholder}
                    onChange={(e) => setDraft({ ...draft, config: { ...draft.config, [f.key]: e.target.value } })} />
                </label>
              ))}

              <label className="f">
                <span>
                  {current.secret_label}
                  {draft.id ? " — laissez vide pour conserver celui déjà enregistré" : ""}
                </span>
                <input className="f" type="password" value={draft.secret} autoComplete="new-password"
                  placeholder={draft.id ? "•••••••• (inchangé)" : ""}
                  onChange={(e) => setDraft({ ...draft, secret: e.target.value })} />
              </label>
              <div className="small muted">
                Le secret est chiffré avant d'être enregistré et ne ressort jamais de l'API : il
                ne réapparaîtra pas dans cet écran.
              </div>
            </div>
            <div className="modal-f">
              <button className="btn ghost" onClick={() => setDraft(null)}>Annuler</button>
              <button className="btn primary" disabled={busy || !draft.name.trim()} onClick={save}>
                Enregistrer
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
