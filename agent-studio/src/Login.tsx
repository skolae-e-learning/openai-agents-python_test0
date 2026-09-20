import { useState } from "react";
import * as auth from "./auth";

export function Login({ onSignedIn }: { onSignedIn: (me: auth.Me) => void }) {
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (mode === "up" && password.length < 8) {
      setError("Mot de passe trop court : 8 caractères au minimum.");
      return;
    }
    setBusy(true);
    try {
      const me = mode === "in"
        ? await auth.signIn(email.trim(), password)
        : await auth.signUp(email.trim(), password);
      onSignedIn(me);
    } catch (err: any) {
      setError(err?.message || "La connexion a échoué.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="card login" onSubmit={submit}>
        <div className="login-brand">
          <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden>
            <circle cx="16" cy="8" r="4.4" fill="var(--accent)" />
            <circle cx="7" cy="24" r="4.4" fill="var(--ink)" />
            <circle cx="25" cy="24" r="4.4" fill="var(--ink)" />
            <path d="M16 12.4 8.6 20M16 12.4 23.4 20" stroke="var(--line)" strokeWidth="1.6" />
          </svg>
          <div>
            <b>Agent Studio</b>
            <span>équipes d'agents IA</span>
          </div>
        </div>

        <p className="small muted" style={{ margin: "0 0 18px", lineHeight: 1.6 }}>
          {mode === "in"
            ? "Connectez-vous pour retrouver vos agents et vos projets."
            : "Créez un compte. Vos agents et projets sont privés par défaut ; vous choisirez ce que vous rendez public."}
        </p>

        <label className="f">
          <span>Adresse électronique</span>
          <input className="f" type="email" required autoComplete="email" autoFocus
            value={email} onChange={(e) => setEmail(e.target.value)} placeholder="vous@exemple.fr" />
        </label>
        <label className="f">
          <span>Mot de passe{mode === "up" ? " — 8 caractères minimum" : ""}</span>
          <input className="f" type="password" required minLength={mode === "up" ? 8 : undefined}
            autoComplete={mode === "in" ? "current-password" : "new-password"}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>

        {error && <div className="banner warn" style={{ marginBottom: 12 }}>{error}</div>}

        <button className="btn primary" type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "…" : mode === "in" ? "Se connecter" : "Créer mon compte"}
        </button>

        <div className="small muted" style={{ marginTop: 14, textAlign: "center" }}>
          {mode === "in" ? "Pas encore de compte ? " : "Vous avez déjà un compte ? "}
          <button type="button" className="btn sm ghost"
            onClick={() => { setMode(mode === "in" ? "up" : "in"); setError(""); }}>
            {mode === "in" ? "En créer un" : "Se connecter"}
          </button>
        </div>
      </form>
    </div>
  );
}
