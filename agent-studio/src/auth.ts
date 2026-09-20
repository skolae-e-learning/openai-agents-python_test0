// Client Neon Auth (Stack Auth) en appels REST directs.
//
// Le SDK officiel pèse 3,6 Mo dépaquetés (Stripe, WebAuthn, rrweb, react-hook-form)
// pour ce dont nous avons besoin : s'inscrire, se connecter, renouveler, se
// déconnecter. Quatre appels, écrits ici, gardent le bundle et l'interface à nous.

const API = "https://api.stack-auth.com/api/v1";
const TOKENS = "agent-studio.tokens";

export type AuthConfig = { ready: boolean; project_id: string; publishable_key: string };
export type Me = { id: string; email: string };

type Tokens = { access: string; refresh: string };

let config: AuthConfig | null = null;
let tokens: Tokens | null = null;

/** Les jetons survivent au rechargement ; un stockage indisponible n'est pas fatal. */
function load(): Tokens | null {
  if (tokens) return tokens;
  try {
    const raw = localStorage.getItem(TOKENS);
    tokens = raw ? JSON.parse(raw) : null;
  } catch {
    tokens = null;
  }
  return tokens;
}

function save(t: Tokens | null) {
  tokens = t;
  try {
    if (t) localStorage.setItem(TOKENS, JSON.stringify(t));
    else localStorage.removeItem(TOKENS);
  } catch {
    /* navigation privée : on reste connecté le temps de la session */
  }
}

export function setConfig(c: AuthConfig) {
  config = c;
}

export function accessToken(): string | null {
  return load()?.access ?? null;
}

export function isSignedIn(): boolean {
  return !!load();
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  if (!config?.project_id) throw new Error("Authentification non configurée sur ce déploiement.");
  return {
    "content-type": "application/json",
    "x-stack-access-type": "client",
    "x-stack-project-id": config.project_id,
    "x-stack-publishable-client-key": config.publishable_key,
    ...extra,
  };
}

/** Messages d'erreur en français : ceux de Stack Auth sont en anglais. */
function translate(code: string, fallback: string): string {
  const table: Record<string, string> = {
    EMAIL_PASSWORD_MISMATCH: "Adresse ou mot de passe incorrect.",
    USER_EMAIL_ALREADY_EXISTS: "Un compte existe déjà avec cette adresse. Connectez-vous.",
    PASSWORD_TOO_SHORT: "Mot de passe trop court : 8 caractères au minimum.",
    PASSWORD_TOO_LONG: "Mot de passe trop long.",
    PASSWORD_REQUIRES_SPECIAL_CHAR: "Le mot de passe doit contenir un caractère spécial.",
    EMAIL_PASSWORD_SIGN_UP_NOT_ENABLED: "L'inscription par mot de passe est désactivée.",
    REDIRECT_URL_NOT_WHITELISTED: "Ce domaine n'est pas autorisé côté Neon Auth.",
    VERIFICATION_CODE_NOT_FOUND: "Lien de vérification invalide ou expiré.",
  };
  return table[code] || fallback || "La connexion a échoué.";
}

async function post(path: string, body: unknown, extra: Record<string, string> = {}) {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: headers(extra),
    body: body === null ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(translate(data.code, data.error));
  return data;
}

export async function signUp(email: string, password: string): Promise<Me> {
  const d = await post("/auth/password/sign-up", {
    email,
    password,
    verification_callback_url: `${location.origin}/`,
  });
  save({ access: d.access_token, refresh: d.refresh_token });
  return { id: d.user_id, email };
}

export async function signIn(email: string, password: string): Promise<Me> {
  const d = await post("/auth/password/sign-in", { email, password });
  save({ access: d.access_token, refresh: d.refresh_token });
  return { id: d.user_id, email };
}

/** Renouvelle le jeton d'accès (durée de vie : une heure). */
export async function refresh(): Promise<boolean> {
  const t = load();
  if (!t?.refresh) return false;
  try {
    const d = await post("/auth/sessions/current/refresh", null, {
      "x-stack-refresh-token": t.refresh,
    });
    save({ access: d.access_token, refresh: t.refresh });
    return true;
  } catch {
    save(null);
    return false;
  }
}

export async function me(): Promise<Me | null> {
  const t = load();
  if (!t) return null;
  const call = async () => {
    const res = await fetch(`${API}/users/me`, {
      headers: headers({ "x-stack-access-token": load()!.access }),
    });
    return res;
  };
  let res = await call();
  if (res.status === 401 && (await refresh())) res = await call();
  if (!res.ok) {
    save(null);
    return null;
  }
  const d = await res.json();
  return { id: d.id, email: d.primary_email || "" };
}

export async function signOut() {
  const t = load();
  save(null);
  if (!t) return;
  try {
    await fetch(`${API}/auth/sessions/current`, {
      method: "DELETE",
      headers: headers({ "x-stack-access-token": t.access, "x-stack-refresh-token": t.refresh }),
    });
  } catch {
    /* la session locale est déjà effacée, c'est ce qui compte */
  }
}
