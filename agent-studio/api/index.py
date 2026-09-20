"""Agent Studio — API.

Déployée comme fonction serverless sur Vercel. Tout tient dans un seul module
pour éviter toute ambiguïté d'import dans l'environnement serverless.

Principe d'exécution : un appel à /api/tick exécute exactement UN step
d'orchestration, puis rend la main. C'est ce qui permet de faire tourner une
équipe d'agents sur une plateforme sans processus long.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import jwt
import psycopg
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from jwt import PyJWKClient
from psycopg.rows import dict_row
from pydantic import BaseModel

# Rempli au déploiement. En production, préférer la variable d'environnement.
_FALLBACK_DSN = ""

DATABASE_URL = os.environ.get("DATABASE_URL") or _FALLBACK_DSN
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Chiffrement des secrets de connexion. Absente, l'application refuse d'enregistrer
# un secret plutôt que de le stocker en clair.
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "")
CONNECTORS_READY = bool(APP_SECRET_KEY)

# Neon Auth (Stack Auth). Ces deux valeurs sont publiques par conception : la clé
# publiable est faite pour vivre dans le navigateur. Le secret de signature, lui,
# ne quitte jamais Stack : on ne fait que vérifier des signatures via JWKS.
STACK_PROJECT_ID = os.environ.get("STACK_PROJECT_ID", "")
STACK_PUBLISHABLE_CLIENT_KEY = os.environ.get("STACK_PUBLISHABLE_CLIENT_KEY", "")
JWKS_URL = (
    f"https://api.stack-auth.com/api/v1/projects/{STACK_PROJECT_ID}/.well-known/jwks.json"
)
AUTH_READY = bool(STACK_PROJECT_ID)

app = FastAPI(title="Agent Studio")


# --------------------------------------------------------------------------
# Authentification
# --------------------------------------------------------------------------

_jwk_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    """Client JWKS mémorisé pour la durée de vie du processus serverless."""
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwk_client


def optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """Identité si un jeton valide est présent, sinon None. Ne lève jamais."""
    if not AUTH_READY or not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    try:
        claims = jwt.decode(
            parts[1],
            _jwks().get_signing_key_from_jwt(parts[1]).key,
            algorithms=["ES256"],
            audience=STACK_PROJECT_ID,
        )
    except Exception:
        return None
    if not claims.get("sub"):
        return None
    return {"id": claims["sub"], "email": claims.get("email") or ""}


def current_user(me: dict | None = Depends(optional_user)) -> dict:
    """Identité obligatoire : toute route de données passe par là."""
    if me is None:
        raise HTTPException(401, "Connexion requise.")
    return me


# --------------------------------------------------------------------------
# Contrôle d'accès
#
# Règle unique : on lit ce qui nous appartient ou ce qui est public, on ne
# modifie que ce qui nous appartient. Les lignes créées avant l'arrivée des
# comptes ont owner_id nul : lisibles par tous, modifiables par personne tant
# qu'un compte ne les a pas réclamées.
# --------------------------------------------------------------------------

LEGACY_HINT = (
    "Cet élément a été créé avant la mise en place des comptes. "
    "Récupérez-le depuis l'écran Agents pour pouvoir le modifier."
)


def _fetch(conn, table: str, row_id: str) -> dict:
    row = q1(conn, f"select * from {table} where id=%s", row_id)
    if not row:
        raise HTTPException(404, "Élément introuvable.")
    return row


def readable(conn, table: str, row_id: str, me: dict) -> dict:
    row = _fetch(conn, table, row_id)
    if row["owner_id"] in (None, me["id"]) or row["visibility"] == "public":
        return row
    raise HTTPException(404, "Élément introuvable.")


def owned(conn, table: str, row_id: str, me: dict) -> dict:
    row = _fetch(conn, table, row_id)
    if row["owner_id"] == me["id"]:
        return row
    if row["owner_id"] is None:
        raise HTTPException(403, LEGACY_HINT)
    raise HTTPException(403, "Cet élément appartient à quelqu'un d'autre.")


# Les écrans « Agents » et « Projets » montrent ce qui m'appartient et ce qui
# précède les comptes — pas le public des autres, qui a son propre écran.
MINE = "(owner_id = %s or owner_id is null)"


def _vis(value: str) -> str:
    """Une visibilité inconnue retombe sur « privé » : le défaut sûr."""
    return "public" if value == "public" else "private"


# --------------------------------------------------------------------------
# Base de données
# --------------------------------------------------------------------------


@contextmanager
def db():
    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL absent")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def q(conn, sql: str, *args) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, args)
        if cur.description is None:
            return []
        return cur.fetchall()


def q1(conn, sql: str, *args) -> dict | None:
    rows = q(conn, sql, *args)
    return rows[0] if rows else None


def now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Connexions externes : secrets, capacités, types
# --------------------------------------------------------------------------

# Taxonomie volontairement courte. C'est elle qui rend les alternatives
# calculables : deux connexions qui couvrent `mail.send` sont interchangeables
# pour l'arbitrage, quel que soit le fournisseur derrière.
CAPABILITIES = [
    "llm.text",
    "llm.image",
    "audio.tts",
    "mail.send",
    "workspace.write",
    "repo.write",
    "deploy",
    "http.generic",
]

# Ce que la publication peut mobiliser. Le step de garde les déclare, `run_step`
# les résout contre les connexions du propriétaire du projet.
PUBLISH_CAPABILITIES = ["mail.send", "workspace.write", "repo.write", "deploy", "http.generic"]

# Chaque type décrit ses capacités et les champs de configuration attendus.
# L'écran Connecteurs est piloté par ces données : ajouter un type ne touche
# pas le front.
CONNECTOR_KINDS: dict[str, dict] = {
    "http": {
        "label": "HTTP générique",
        "capabilities": ["http.generic"],
        "secret_label": "Jeton (optionnel, envoyé en Authorization: Bearer)",
        "secret_required": False,
        "fields": [
            {"key": "url", "label": "URL appelée", "required": True,
             "placeholder": "https://exemple.test/hook"},
            {"key": "headers", "label": "En-têtes supplémentaires (JSON)", "required": False,
             "placeholder": '{"X-Token": "…"}'},
        ],
        "help": "Rattrapage universel : tout service acceptant un POST devient utilisable.",
    },
    "resend": {
        "label": "Courriel (Resend)",
        "capabilities": ["mail.send"],
        "secret_label": "Clé API Resend",
        "secret_required": True,
        "fields": [
            {"key": "from", "label": "Adresse d'envoi", "required": True,
             "placeholder": "studio@votre-domaine.fr"},
            {"key": "to", "label": "Destinataire", "required": True,
             "placeholder": "vous@exemple.fr"},
        ],
        "help": "L'adresse d'envoi doit appartenir à un domaine vérifié chez Resend.",
    },
    "notion": {
        "label": "Notion",
        "capabilities": ["workspace.write"],
        "secret_label": "Jeton d'intégration interne",
        "secret_required": True,
        "fields": [
            {"key": "parent_page_id", "label": "Page parente", "required": True,
             "placeholder": "2f0e…  (identifiant de la page)"},
        ],
        "help": "La page parente doit être partagée avec l'intégration côté Notion.",
    },
    "github": {
        "label": "GitHub",
        "capabilities": ["repo.write"],
        "secret_label": "Jeton d'accès personnel",
        "secret_required": True,
        "fields": [
            {"key": "repo", "label": "Dépôt", "required": True, "placeholder": "compte/depot"},
            {"key": "path", "label": "Dossier de destination", "required": False,
             "placeholder": "productions"},
            {"key": "branch", "label": "Branche", "required": False, "placeholder": "main"},
        ],
        "help": "Le jeton doit porter le droit d'écriture sur le contenu du dépôt.",
    },
    "vercel": {
        "label": "Vercel",
        "capabilities": ["deploy"],
        "secret_label": "Jeton d'API",
        "secret_required": True,
        "fields": [
            {"key": "deploy_hook_url", "label": "URL du hook de déploiement", "required": True,
             "placeholder": "https://api.vercel.com/v1/integrations/deploy/…"},
        ],
        "help": "Le hook se crée dans les réglages Git du projet Vercel.",
    },
    "openai": {
        "label": "OpenAI",
        "capabilities": ["llm.text", "llm.image"],
        "secret_label": "Clé API",
        "secret_required": True,
        "fields": [],
        "help": "Fait passer vos cycles en mode réel, sans variable d'environnement serveur.",
    },
    "anthropic": {
        "label": "Anthropic",
        "capabilities": ["llm.text"],
        "secret_label": "Clé API",
        "secret_required": True,
        "fields": [],
        "help": "Enregistrée et testée ; la production de contenu passe aujourd'hui par OpenAI.",
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "capabilities": ["audio.tts"],
        "secret_label": "Clé API",
        "secret_required": True,
        "fields": [],
        "help": "Synthèse vocale, mobilisable à la publication.",
    },
}


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(APP_SECRET_KEY.encode())


def seal(plain: str) -> str:
    """Chiffre un secret. Sans clé de chiffrement, on refuse plutôt que stocker en clair."""
    if not plain:
        return ""
    if not CONNECTORS_READY:
        raise HTTPException(
            500,
            "APP_SECRET_KEY n'est pas configurée sur le déploiement : impossible "
            "d'enregistrer un secret en sécurité.",
        )
    return _fernet().encrypt(plain.encode()).decode()


def unseal(token: str) -> str:
    """Déchiffre un secret. Une valeur illisible vaut secret absent, jamais une exception."""
    if not token or not CONNECTORS_READY:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        return ""


def own_connector(conn, connector_id: str, me: dict) -> dict:
    """Une connexion n'est jamais publique : `readable()` ne s'applique pas ici."""
    row = q1(conn, "select * from connectors where id=%s", connector_id)
    if not row or row["owner_id"] != me["id"]:
        raise HTTPException(404, "Connexion introuvable.")
    return row


def public_connector(row: dict) -> dict:
    """Vue sortante d'une connexion : le secret n'en sort jamais."""
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "label": CONNECTOR_KINDS.get(row["kind"], {}).get("label", row["kind"]),
        "name": row["name"],
        "capabilities": list(row["capabilities"] or []),
        "config": row["config"] or {},
        "secret_set": bool(row["secret_enc"]),
        "status": row["status"],
        "status_detail": row["status_detail"],
        "last_tested_at": row["last_tested_at"],
    }


def connectors_covering(conn, owner_id: str | None, capabilities: list[str]) -> list[dict]:
    """Connexions du propriétaire couvrant au moins une des capacités demandées."""
    if not owner_id or not capabilities:
        return []
    rows = q(
        conn,
        "select * from connectors where owner_id=%s and capabilities && %s order by name",
        owner_id,
        capabilities,
    )
    return rows


def llm_key_for(conn, owner_id: str | None) -> str:
    """Clé OpenAI à utiliser : celle du propriétaire si elle existe, sinon celle du serveur."""
    if owner_id:
        row = q1(
            conn,
            """select secret_enc from connectors
               where owner_id=%s and kind='openai' and secret_enc <> ''
               order by (status='ok') desc, created_at limit 1""",
            owner_id,
        )
        if row:
            key = unseal(row["secret_enc"])
            if key:
                return key
    return OPENAI_API_KEY


def _extra_headers(config: dict) -> dict:
    """En-têtes saisis à la main. Une saisie invalide est ignorée, pas fatale."""
    raw = config.get("headers") or ""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    try:
        parsed = json.loads(raw) if str(raw).strip() else {}
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _request(method: str, url: str, *, headers=None, json_body=None, timeout: float = 15.0):
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        return client.request(method, url, headers=headers or {}, json=json_body)


def probe_connector(row: dict) -> tuple[bool, str]:
    """Un appel minimal et sans effet de bord, pour dire si la connexion répond.

    Toute erreur réseau devient un message lisible : un test qui échoue est une
    information, pas une panne de l'application.
    """
    kind = row["kind"]
    secret = unseal(row["secret_enc"])
    config = row["config"] or {}
    if CONNECTOR_KINDS.get(kind, {}).get("secret_required") and not secret:
        return False, "Aucun secret enregistré, ou secret illisible avec la clé actuelle."

    try:
        if kind == "openai":
            r = _request("GET", "https://api.openai.com/v1/models",
                         headers={"Authorization": f"Bearer {secret}"})
        elif kind == "anthropic":
            r = _request("GET", "https://api.anthropic.com/v1/models",
                         headers={"x-api-key": secret, "anthropic-version": "2023-06-01"})
        elif kind == "elevenlabs":
            r = _request("GET", "https://api.elevenlabs.io/v1/user",
                         headers={"xi-api-key": secret})
        elif kind == "resend":
            r = _request("GET", "https://api.resend.com/domains",
                         headers={"Authorization": f"Bearer {secret}"})
        elif kind == "notion":
            r = _request("GET", "https://api.notion.com/v1/users/me",
                         headers={"Authorization": f"Bearer {secret}",
                                  "Notion-Version": "2022-06-28"})
        elif kind == "github":
            r = _request("GET", "https://api.github.com/user",
                         headers={"Authorization": f"Bearer {secret}",
                                  "Accept": "application/vnd.github+json"})
        elif kind == "vercel":
            r = _request("GET", "https://api.vercel.com/v2/user",
                         headers={"Authorization": f"Bearer {secret}"})
        elif kind == "http":
            # Volontairement en GET : tester ne doit pas déclencher l'action.
            headers = _extra_headers(config)
            if secret:
                headers.setdefault("Authorization", f"Bearer {secret}")
            r = _request("GET", str(config.get("url", "")), headers=headers)
            # Un webhook qui n'accepte que POST répond 405 : il est joignable.
            if r.status_code < 500:
                return True, f"Joignable (HTTP {r.status_code})."
            return False, f"Le service répond HTTP {r.status_code}."
        else:
            return False, f"Type de connexion inconnu : « {kind} »."
    except Exception as exc:
        return False, f"Contact impossible : {str(exc)[:200]}"

    if r.status_code < 300:
        return True, "Connexion établie."
    if r.status_code in (401, 403):
        return False, f"Refusé (HTTP {r.status_code}) : le secret est invalide ou insuffisant."
    return False, f"Réponse inattendue : HTTP {r.status_code}. {r.text[:160]}"


def publish_through(row: dict, project: dict, cycle: dict, artifacts: list[dict]) -> tuple[bool, str]:
    """Exécute réellement la publication via une connexion, et dit ce qui s'est passé.

    Appelée seulement après une approbation humaine explicite, et seulement pour
    les connexions que l'humain a laissées cochées.
    """
    kind = row["kind"]
    secret = unseal(row["secret_enc"])
    config = row["config"] or {}
    title = f"{project['name']} — {cycle.get('brief') or 'cycle terminé'}"
    body = "\n\n".join(f"## {a['title']}\n\n{a['body']}" for a in artifacts) or "(aucune production)"

    try:
        if kind == "http":
            headers = _extra_headers(config)
            if secret:
                headers.setdefault("Authorization", f"Bearer {secret}")
            r = _request(
                "POST", str(config.get("url", "")), headers=headers,
                json_body={
                    "project": project["name"],
                    "objective": project.get("objective", ""),
                    "cycle_id": str(cycle["id"]),
                    "brief": cycle.get("brief", ""),
                    "artifacts": [
                        {"title": a["title"], "agent": a["agent_name"], "body": a["body"]}
                        for a in artifacts
                    ],
                },
            )
        elif kind == "resend":
            r = _request(
                "POST", "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {secret}"},
                json_body={"from": config.get("from", ""), "to": [config.get("to", "")],
                           "subject": title, "text": body},
            )
        elif kind == "notion":
            # Un bloc par production : Notion plafonne un paragraphe à 2000 caractères.
            children = [
                {"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk[:2000]}}]}}
                for a in artifacts
                for chunk in (f"{a['title']} — {a['body']}",)
            ] or [{"object": "block", "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text", "text": {"content": "(aucune production)"}}]}}]
            r = _request(
                "POST", "https://api.notion.com/v1/pages",
                headers={"Authorization": f"Bearer {secret}", "Notion-Version": "2022-06-28"},
                json_body={
                    "parent": {"page_id": str(config.get("parent_page_id", ""))},
                    "properties": {"title": [{"type": "text", "text": {"content": title[:200]}}]},
                    "children": children[:90],
                },
            )
        elif kind == "github":
            import base64 as _b64

            folder = str(config.get("path", "") or "productions").strip("/")
            path = f"{folder}/{str(cycle['id'])[:8]}.md"
            payload = {
                "message": f"Agent Studio — {title}"[:200],
                "content": _b64.b64encode(f"# {title}\n\n{body}".encode()).decode(),
            }
            if str(config.get("branch", "")).strip():
                payload["branch"] = str(config["branch"]).strip()
            r = _request(
                "PUT", f"https://api.github.com/repos/{config.get('repo', '')}/contents/{path}",
                headers={"Authorization": f"Bearer {secret}",
                         "Accept": "application/vnd.github+json"},
                json_body=payload,
            )
        elif kind == "vercel":
            r = _request("POST", str(config.get("deploy_hook_url", "")), json_body={})
        elif kind in ("openai", "anthropic", "elevenlabs"):
            return False, "Cette connexion sert à produire du contenu, pas à publier."
        else:
            return False, f"Type de connexion inconnu : « {kind} »."
    except Exception as exc:
        return False, f"Échec : {str(exc)[:200]}"

    if r.status_code < 300:
        return True, f"Envoyé (HTTP {r.status_code})."
    return False, f"Refusé : HTTP {r.status_code}. {r.text[:160]}"


# --------------------------------------------------------------------------
# Modèles d'entrée
# --------------------------------------------------------------------------


class AgentIn(BaseModel):
    name: str
    description: str = ""
    role: str = "specialist"
    instructions: str = ""
    model: str = "gpt-4.1-mini"
    temperature: float | None = None
    accent: str = "slate"
    visibility: str = "private"


class ProjectIn(BaseModel):
    name: str
    objective: str = ""
    max_revisions: int = 2
    visibility: str = "private"


class TeamMember(BaseModel):
    agent_id: str
    role: str = "specialist"
    x: float = 0
    y: float = 0


class Edge(BaseModel):
    source: str
    target: str
    kind: str = "calls"


class CycleIn(BaseModel):
    brief: str = ""


class ControlIn(BaseModel):
    action: str
    text: str = ""


class DecisionIn(BaseModel):
    status: str
    response: str = ""
    # Connexions retenues par l'humain au moment d'autoriser. Absente, aucune
    # n'est mobilisée : une approbation ne déclenche rien par défaut.
    connectors: list[str] = []


class MemoryIn(BaseModel):
    content: str


class ConnectorIn(BaseModel):
    kind: str
    name: str
    config: dict = {}
    # Vide sur une mise à jour : le secret déjà enregistré est conservé.
    secret: str = ""


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


def log(conn, project_id, cycle_id, *, kind, title, body="", agent=None, depth=0, payload=None):
    q(
        conn,
        """insert into messages (project_id, cycle_id, agent_id, agent_name, kind, depth,
                                 title, body, payload)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        project_id,
        cycle_id,
        (agent or {}).get("id"),
        (agent or {}).get("name", ""),
        kind,
        depth,
        title,
        body,
        json.dumps(payload or {}),
    )


# --------------------------------------------------------------------------
# Routes — état, agents, mémoire
# --------------------------------------------------------------------------


@app.get("/api/state")
def state(me: dict | None = Depends(optional_user)):
    ok, detail = True, "connectée"
    # Le mode réel n'est plus une propriété du serveur : une connexion OpenAI
    # personnelle suffit, la variable d'environnement sert de repli.
    live = bool(OPENAI_API_KEY)
    try:
        with db() as conn:
            q1(conn, "select 1 as ok")
            if me:
                live = bool(llm_key_for(conn, me["id"]))
    except Exception as exc:  # pragma: no cover - dépend de l'infra
        ok, detail = False, str(exc)[:200]
    return {
        "db": ok,
        "db_detail": detail,
        "live": live,
        "mode": "live" if live else "demo",
        "connectors_ready": CONNECTORS_READY,
        # Servie à l'exécution plutôt que figée au build : changer de projet Auth
        # ne demande pas de reconstruire le front.
        "auth": {
            "ready": AUTH_READY,
            "project_id": STACK_PROJECT_ID,
            "publishable_key": STACK_PUBLISHABLE_CLIENT_KEY,
        },
    }


@app.get("/api/summary")
def summary(me: dict = Depends(current_user)):
    """Compteurs de mes projets, affichés en permanence dans la barre latérale."""
    with db() as conn:
        row = q(
            conn,
            f"""select
                 count(*) filter (where state='RUNNING') as running,
                 count(*) filter (where state='PAUSED')  as paused
               from projects where {MINE}""",
            me["id"],
        )[0]
        pending = q(
            conn,
            f"""select count(*) as n from decisions d
                join projects p on p.id = d.project_id
                where d.status='pending' and {MINE.replace("owner_id", "p.owner_id")}""",
            me["id"],
        )[0]["n"]
        return {"running": row["running"], "paused": row["paused"], "pending": pending}


@app.get("/api/agents")
def list_agents(me: dict = Depends(current_user)):
    with db() as conn:
        return q(conn, f"select * from agents where {MINE} order by created_at", me["id"])


@app.post("/api/agents")
def create_agent(body: AgentIn, me: dict = Depends(current_user)):
    with db() as conn:
        return q1(
            conn,
            """insert into agents (owner_id, visibility, name, description, role,
                                   instructions, model, temperature, accent)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
            me["id"],
            _vis(body.visibility),
            body.name,
            body.description,
            body.role,
            body.instructions,
            body.model,
            body.temperature,
            body.accent,
        )


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentIn, me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "agents", agent_id, me)
        row = q1(
            conn,
            """update agents set name=%s, description=%s, role=%s, instructions=%s,
                                 model=%s, temperature=%s, accent=%s, visibility=%s,
                                 updated_at=now()
               where id=%s returning *""",
            body.name,
            body.description,
            body.role,
            body.instructions,
            body.model,
            body.temperature,
            body.accent,
            _vis(body.visibility),
            agent_id,
        )
        if not row:
            raise HTTPException(404, "Agent introuvable.")
        return row


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "agents", agent_id, me)
        q(conn, "delete from agents where id=%s", agent_id)
    return {"ok": True}


@app.get("/api/agents/{agent_id}/memory")
def get_memory(agent_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        readable(conn, "agents", agent_id, me)
        return q(conn, "select * from agent_memory where agent_id=%s order by created_at desc", agent_id)


@app.post("/api/agents/{agent_id}/memory")
def add_memory(agent_id: str, body: MemoryIn, me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "agents", agent_id, me)
        return q1(
            conn,
            "insert into agent_memory (agent_id, content) values (%s,%s) returning *",
            agent_id,
            body.content,
        )


@app.delete("/api/memory/{memory_id}")
def del_memory(memory_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        row = q1(conn, "select agent_id from agent_memory where id=%s", memory_id)
        if not row:
            raise HTTPException(404, "Souvenir introuvable.")
        owned(conn, "agents", row["agent_id"], me)
        q(conn, "delete from agent_memory where id=%s", memory_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# Routes — connexions externes
# --------------------------------------------------------------------------


@app.get("/api/connectors/kinds")
def connector_kinds():
    """Catalogue des types. L'écran est construit à partir de cette réponse."""
    return {
        "ready": CONNECTORS_READY,
        "capabilities": CAPABILITIES,
        "kinds": [{"kind": k, **v} for k, v in CONNECTOR_KINDS.items()],
    }


@app.get("/api/connectors")
def list_connectors(me: dict = Depends(current_user)):
    with db() as conn:
        rows = q(conn, "select * from connectors where owner_id=%s order by created_at", me["id"])
        return [public_connector(r) for r in rows]


def _check_connector_in(body: ConnectorIn) -> dict:
    """Valide le type et les champs obligatoires, renvoie la définition du type."""
    kind = CONNECTOR_KINDS.get(body.kind)
    if not kind:
        raise HTTPException(400, f"Type de connexion inconnu : « {body.kind} ».")
    if not body.name.strip():
        raise HTTPException(400, "Donnez un nom à cette connexion.")
    for field in kind["fields"]:
        if field["required"] and not str(body.config.get(field["key"], "")).strip():
            raise HTTPException(400, f"Le champ « {field['label']} » est obligatoire.")
    return kind


@app.post("/api/connectors")
def create_connector(body: ConnectorIn, me: dict = Depends(current_user)):
    kind = _check_connector_in(body)
    if kind["secret_required"] and not body.secret.strip():
        raise HTTPException(400, f"Le champ « {kind['secret_label']} » est obligatoire.")
    with db() as conn:
        row = q1(
            conn,
            """insert into connectors (owner_id, kind, name, capabilities, config, secret_enc)
               values (%s,%s,%s,%s,%s,%s) returning *""",
            me["id"],
            body.kind,
            body.name.strip(),
            kind["capabilities"],
            json.dumps(body.config),
            seal(body.secret.strip()),
        )
        return public_connector(row)


@app.patch("/api/connectors/{connector_id}")
def update_connector(connector_id: str, body: ConnectorIn, me: dict = Depends(current_user)):
    kind = _check_connector_in(body)
    with db() as conn:
        existing = own_connector(conn, connector_id, me)
        # Secret vide : on garde celui déjà enregistré plutôt que de l'effacer.
        secret_enc = seal(body.secret.strip()) if body.secret.strip() else existing["secret_enc"]
        if kind["secret_required"] and not secret_enc:
            raise HTTPException(400, f"Le champ « {kind['secret_label']} » est obligatoire.")
        row = q1(
            conn,
            """update connectors
               set kind=%s, name=%s, capabilities=%s, config=%s, secret_enc=%s,
                   status='untested', status_detail='', updated_at=now()
               where id=%s returning *""",
            body.kind,
            body.name.strip(),
            kind["capabilities"],
            json.dumps(body.config),
            secret_enc,
            connector_id,
        )
        return public_connector(row)


@app.delete("/api/connectors/{connector_id}")
def delete_connector(connector_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        own_connector(conn, connector_id, me)
        q(conn, "delete from connectors where id=%s", connector_id)
    return {"ok": True}


@app.post("/api/connectors/{connector_id}/test")
def test_connector(connector_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        row = own_connector(conn, connector_id, me)
        ok, detail = probe_connector(row)
        q(
            conn,
            """update connectors set status=%s, status_detail=%s, last_tested_at=now()
               where id=%s""",
            "ok" if ok else "error",
            detail[:400],
            connector_id,
        )
        return public_connector(q1(conn, "select * from connectors where id=%s", connector_id))


# --------------------------------------------------------------------------
# Routes — projets
# --------------------------------------------------------------------------


@app.get("/api/projects")
def list_projects(me: dict = Depends(current_user)):
    with db() as conn:
        return q(
            conn,
            f"""select p.*, (select count(*) from project_agents pa where pa.project_id=p.id) as team_size,
                      (select count(*) from artifacts a where a.project_id=p.id) as artifact_count,
                      (select count(*) from decisions d where d.project_id=p.id and d.status='pending')
                        as pending_count
               from projects p where {MINE.replace("owner_id", "p.owner_id")}
               order by p.created_at desc""",
            me["id"],
        )


@app.post("/api/projects")
def create_project(body: ProjectIn, me: dict = Depends(current_user)):
    with db() as conn:
        return q1(
            conn,
            """insert into projects (owner_id, visibility, name, objective, max_revisions)
               values (%s,%s,%s,%s,%s) returning *""",
            me["id"],
            _vis(body.visibility),
            body.name,
            body.objective,
            body.max_revisions,
        )


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectIn, me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "projects", project_id, me)
        return q1(
            conn,
            """update projects set name=%s, objective=%s, max_revisions=%s,
                                   visibility=%s, updated_at=now()
               where id=%s returning *""",
            body.name,
            body.objective,
            body.max_revisions,
            _vis(body.visibility),
            project_id,
        )


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "projects", project_id, me)
        q(conn, "delete from projects where id=%s", project_id)
    return {"ok": True}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        project = readable(conn, "projects", project_id, me)
        team = q(
            conn,
            """select pa.role as team_role, pa.x, pa.y, a.*
               from project_agents pa join agents a on a.id=pa.agent_id
               where pa.project_id=%s order by pa.role desc, a.name""",
            project_id,
        )
        edges = q(conn, "select * from architecture_edges where project_id=%s", project_id)
        cycle = q1(
            conn,
            "select * from cycles where project_id=%s order by created_at desc limit 1",
            project_id,
        )
        return {"project": project, "team": team, "edges": edges, "cycle": cycle}


@app.put("/api/projects/{project_id}/team")
def set_team(project_id: str, body: list[TeamMember], me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "projects", project_id, me)
        # On ne compose une équipe qu'avec des agents qu'on a le droit de lire.
        for m in body:
            readable(conn, "agents", m.agent_id, me)
        q(conn, "delete from project_agents where project_id=%s", project_id)
        for m in body:
            q(
                conn,
                """insert into project_agents (project_id, agent_id, role, x, y)
                   values (%s,%s,%s,%s,%s)""",
                project_id,
                m.agent_id,
                m.role,
                m.x,
                m.y,
            )
        q(
            conn,
            """delete from architecture_edges where project_id=%s and (
                 source_agent_id not in (select agent_id from project_agents where project_id=%s)
                 or target_agent_id not in (select agent_id from project_agents where project_id=%s))""",
            project_id,
            project_id,
            project_id,
        )
    return {"ok": True}


@app.put("/api/projects/{project_id}/edges")
def set_edges(project_id: str, body: list[Edge], me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "projects", project_id, me)
        q(conn, "delete from architecture_edges where project_id=%s", project_id)
        for e in body:
            q(
                conn,
                """insert into architecture_edges (project_id, source_agent_id, target_agent_id, kind)
                   values (%s,%s,%s,%s) on conflict do nothing""",
                project_id,
                e.source,
                e.target,
                e.kind,
            )
    return {"ok": True}


@app.get("/api/projects/{project_id}/suggestions")
def suggestions(project_id: str, me: dict = Depends(current_user)):
    """Agents existants que l'application propose d'ajouter au projet.

    L'application propose, elle ne recompose jamais l'équipe toute seule.
    """
    with db() as conn:
        project = readable(conn, "projects", project_id, me)
        candidates = q(
            conn,
            f"""select * from agents where {MINE} and id not in
                 (select agent_id from project_agents where project_id=%s)""",
            me["id"],
            project_id,
        )
        objective = (project["objective"] + " " + project["name"]).lower()
        words = {w for w in objective.replace(",", " ").split() if len(w) > 4}
        out = []
        for c in candidates:
            hay = (c["name"] + " " + c["description"] + " " + c["instructions"]).lower()
            hits = sorted(w for w in words if w in hay)
            if hits:
                out.append(
                    {
                        "agent": c,
                        "reason": "Recoupe l'objectif du projet sur : " + ", ".join(hits[:4]),
                        "score": len(hits),
                    }
                )
        out.sort(key=lambda r: -r["score"])
        return out[:4]


# --------------------------------------------------------------------------
# Routes — flux, artefacts, décisions
# --------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/stream")
def stream(project_id: str, after: int = 0, me: dict = Depends(current_user)):
    with db() as conn:
        readable(conn, "projects", project_id, me)
        messages = q(
            conn,
            "select * from messages where project_id=%s and id>%s order by id limit 300",
            project_id,
            after,
        )
        cycle = q1(
            conn,
            "select * from cycles where project_id=%s order by created_at desc limit 1",
            project_id,
        )
        project = q1(conn, "select * from projects where id=%s", project_id)
        pending = q(
            conn,
            "select * from decisions where project_id=%s and status='pending' order by created_at",
            project_id,
        )
        artifacts = q(
            conn,
            "select * from artifacts where project_id=%s order by created_at desc limit 60",
            project_id,
        )
        return {
            "messages": messages,
            "cycle": cycle,
            "project": project,
            "decisions": pending,
            "artifacts": artifacts,
        }


def run_publication(conn, decision, cycle, connector_ids: list[str], me: dict) -> None:
    """Exécute la publication par les connexions laissées cochées.

    L'échec d'une connexion est journalisé et n'interrompt pas le cycle : une
    diffusion ratée ne doit pas détruire le travail déjà produit. Ce qui a été
    décoché est dit explicitement, pour que le journal garde trace du choix.
    """
    project = q1(conn, "select * from projects where id=%s", decision["project_id"])
    proposed = {c["id"] for c in (decision["payload"] or {}).get("connectors", [])}
    chosen = [cid for cid in connector_ids if cid in proposed]
    artifacts = q(
        conn,
        "select agent_name, title, body from artifacts where cycle_id=%s order by created_at",
        cycle["id"],
    )

    declined = proposed - set(chosen)
    if declined:
        names = [
            c["name"]
            for c in (decision["payload"] or {}).get("connectors", [])
            if c["id"] in declined
        ]
        log(conn, project["id"], cycle["id"], kind="human", depth=1,
            title="Connexions écartées à la publication",
            body="Non mobilisées sur votre décision : " + ", ".join(names) + ".")

    for connector_id in chosen:
        row = q1(
            conn,
            "select * from connectors where id=%s and owner_id=%s",
            connector_id,
            me["id"],
        )
        if not row:
            continue
        ok, detail = publish_through(row, project, cycle, artifacts)
        log(conn, project["id"], cycle["id"], kind="result" if ok else "system", depth=1,
            title=("✓ Publié via " if ok else "✗ Échec de publication via ") + row["name"],
            body=detail, payload={"connector": row["name"], "kind": row["kind"], "ok": ok})


@app.post("/api/decisions/{decision_id}/respond")
def respond(decision_id: str, body: DecisionIn, me: dict = Depends(current_user)):
    with db() as conn:
        d = q1(conn, "select * from decisions where id=%s", decision_id)
        if not d:
            raise HTTPException(404, "Décision introuvable.")
        owned(conn, "projects", d["project_id"], me)
        q(
            conn,
            "update decisions set status=%s, response=%s, resolved_at=now() where id=%s",
            body.status,
            body.response,
            decision_id,
        )
        approved = body.status == "approved"
        log(
            conn,
            d["project_id"],
            d["cycle_id"],
            kind="human",
            title="Arbitrage humain : " + ("approuvé" if approved else "rejeté"),
            body=body.response,
        )
        cycle = q1(conn, "select * from cycles where id=%s", d["cycle_id"])
        # Appelée même sans connexion retenue : tout décocher est une décision,
        # et le journal doit en garder la trace.
        if approved and cycle:
            run_publication(conn, d, cycle, body.connectors, me)
        if cycle and cycle["status"] == "waiting_human":
            plan = cycle["plan"]
            cursor = cycle["cursor"]
            if approved:
                q(conn, "update cycles set status='running', cursor=%s where id=%s", cursor + 1, cycle["id"])
            else:
                q(
                    conn,
                    "update cycles set status='done', ended_at=now(), note=%s where id=%s",
                    "Arrêté par un rejet humain.",
                    cycle["id"],
                )
                log(
                    conn,
                    d["project_id"],
                    cycle["id"],
                    kind="system",
                    title="Cycle interrompu",
                    body="Le rejet humain met fin au cycle. Les contenus déjà produits sont conservés.",
                )
            if approved and cursor + 1 < len(plan):
                enqueue(conn, d["project_id"], cycle["id"])
            elif approved:
                q(conn, "update cycles set status='done', ended_at=now() where id=%s", cycle["id"])
                q(conn, "update projects set state='IDLE' where id=%s", d["project_id"])
    return {"ok": True}


@app.post("/api/projects/{project_id}/control")
def control(project_id: str, body: ControlIn, me: dict = Depends(current_user)):
    action = body.action
    with db() as conn:
        project = owned(conn, "projects", project_id, me)
        cycle = q1(
            conn, "select * from cycles where project_id=%s order by created_at desc limit 1", project_id
        )
        if action == "pause":
            q(conn, "update projects set state='PAUSED' where id=%s", project_id)
            log(conn, project_id, cycle and cycle["id"], kind="system", title="⏸ Mise en pause",
                body="Le step en cours se termine, aucun nouveau step n'est planifié.")
        elif action == "resume":
            q(conn, "update projects set state='RUNNING' where id=%s", project_id)
            log(conn, project_id, cycle and cycle["id"], kind="system", title="▶ Reprise",
                body="Le cycle reprend là où il s'était arrêté.")
            if cycle and cycle["status"] == "running":
                enqueue(conn, project_id, cycle["id"])
        elif action == "stop":
            q(conn, "update projects set state='IDLE' where id=%s", project_id)
            if cycle:
                q(conn, "update cycles set status='stopped', ended_at=now() where id=%s", cycle["id"])
                q(conn, "delete from jobs where cycle_id=%s and status='queued'", cycle["id"])
            log(conn, project_id, cycle and cycle["id"], kind="system", title="⛔ Arrêt",
                body="Le cycle est arrêté. L'état reste consultable et les contenus produits sont conservés.")
        elif action == "intervene":
            log(conn, project_id, cycle and cycle["id"], kind="human", title="👤 Intervention",
                body=body.text)
            if cycle and cycle["status"] in ("running", "waiting_human"):
                plan = cycle["plan"]
                cursor = min(cycle["cursor"], max(len(plan) - 1, 0))
                for step in plan[cursor:]:
                    step["directive"] = body.text
                q(conn, "update cycles set plan=%s where id=%s", json.dumps(plan), cycle["id"])
                log(conn, project_id, cycle["id"], kind="system",
                    title="Instruction intégrée au plan",
                    body="Les steps restants tiennent compte de l'instruction humaine.")
        else:
            raise HTTPException(400, "action inconnue")
    return {"ok": True}


# --------------------------------------------------------------------------
# Partage public : explorer, dupliquer, récupérer l'existant
# --------------------------------------------------------------------------


@app.get("/api/explore")
def explore(
    type: str = "agent",
    q_: str = "",
    role: str = "",
    me: dict = Depends(current_user),
):
    """Éléments publics des autres comptes, filtrables.

    On exclut ses propres éléments : ils sont déjà dans « Agents » et « Projets ».
    """
    like = f"%{q_.strip()}%"
    with db() as conn:
        if type == "project":
            rows = q(
                conn,
                """select p.id, p.name, p.objective, p.created_at, p.owner_id,
                          coalesce(u.name, u.email, 'un autre compte') as owner_label,
                          (select count(*) from project_agents pa where pa.project_id=p.id) as team_size
                   from projects p
                   left join neon_auth.users_sync u on u.id = p.owner_id
                   where p.visibility='public' and coalesce(p.owner_id,'') <> %s
                     and (%s = '' or p.name ilike %s or p.objective ilike %s)
                   order by p.created_at desc limit 60""",
                me["id"], q_.strip(), like, like,
            )
        else:
            rows = q(
                conn,
                """select a.id, a.name, a.description, a.role, a.model, a.accent,
                          a.created_at, a.owner_id,
                          coalesce(u.name, u.email, 'un autre compte') as owner_label
                   from agents a
                   left join neon_auth.users_sync u on u.id = a.owner_id
                   where a.visibility='public' and coalesce(a.owner_id,'') <> %s
                     and (%s = '' or a.role = %s)
                     and (%s = '' or a.name ilike %s or a.description ilike %s)
                   order by a.created_at desc limit 60""",
                me["id"], role, role, q_.strip(), like, like,
            )
        return rows


def _copy_agent(conn, src: dict, me: dict, suffix: str = " (copie)") -> dict:
    """Copie autonome d'un agent, mémoire comprise, au nom du compte courant."""
    new = q1(
        conn,
        """insert into agents (owner_id, visibility, name, description, role,
                               instructions, model, temperature, accent)
           values (%s,'private',%s,%s,%s,%s,%s,%s,%s) returning *""",
        me["id"],
        (src["name"] + suffix)[:200],
        src["description"],
        src["role"],
        src["instructions"],
        src["model"],
        src["temperature"],
        src["accent"],
    )
    for m in q(
        conn, "select content from agent_memory where agent_id=%s", src["id"]
    ):
        q(
            conn,
            "insert into agent_memory (agent_id, content, source) values (%s,%s,'copie')",
            new["id"],
            m["content"],
        )
    return new


@app.post("/api/agents/{agent_id}/duplicate")
def duplicate_agent(agent_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        src = readable(conn, "agents", agent_id, me)
        return _copy_agent(conn, src, me)


@app.post("/api/projects/{project_id}/duplicate")
def duplicate_project(project_id: str, me: dict = Depends(current_user)):
    """Copie profonde : le projet dupliqué ne dépend plus de l'original.

    Les agents de l'équipe sont copiés eux aussi, sinon modifier un agent chez
    soi reviendrait à modifier celui de quelqu'un d'autre.
    """
    with db() as conn:
        src = readable(conn, "projects", project_id, me)
        new = q1(
            conn,
            """insert into projects (owner_id, visibility, name, objective, max_revisions)
               values (%s,'private',%s,%s,%s) returning *""",
            me["id"],
            (src["name"] + " (copie)")[:200],
            src["objective"],
            src["max_revisions"],
        )
        mapping: dict[str, str] = {}
        for member in q(
            conn,
            """select pa.role as team_role, pa.x, pa.y, a.*
               from project_agents pa join agents a on a.id = pa.agent_id
               where pa.project_id=%s""",
            project_id,
        ):
            copy = _copy_agent(conn, member, me, suffix="")
            mapping[str(member["id"])] = str(copy["id"])
            q(
                conn,
                """insert into project_agents (project_id, agent_id, role, x, y)
                   values (%s,%s,%s,%s,%s)""",
                new["id"], copy["id"], member["team_role"], member["x"], member["y"],
            )
        for e in q(
            conn, "select * from architecture_edges where project_id=%s", project_id
        ):
            src_id = mapping.get(str(e["source_agent_id"]))
            dst_id = mapping.get(str(e["target_agent_id"]))
            if src_id and dst_id:
                q(
                    conn,
                    """insert into architecture_edges
                         (project_id, source_agent_id, target_agent_id, kind)
                       values (%s,%s,%s,%s) on conflict do nothing""",
                    new["id"], src_id, dst_id, e["kind"],
                )
        return new


@app.get("/api/legacy")
def legacy_count(me: dict = Depends(current_user)):
    """Combien d'éléments datent d'avant les comptes et attendent un propriétaire."""
    with db() as conn:
        return {
            "agents": q(conn, "select count(*) as n from agents where owner_id is null")[0]["n"],
            "projects": q(conn, "select count(*) as n from projects where owner_id is null")[0]["n"],
        }


@app.post("/api/legacy/claim")
def claim_legacy(me: dict = Depends(current_user)):
    """Rattache à mon compte tout ce qui a été créé avant les comptes.

    Premier arrivé, premier servi : une fois réclamés, ces éléments ont un
    propriétaire et ne sont plus visibles par les autres.
    """
    with db() as conn:
        a = q(conn, "update agents set owner_id=%s where owner_id is null returning id", me["id"])
        p = q(conn, "update projects set owner_id=%s where owner_id is null returning id", me["id"])
        return {"agents": len(a), "projects": len(p)}


# --------------------------------------------------------------------------
# Cycle : création et exécution pas à pas
# --------------------------------------------------------------------------


def enqueue(conn, project_id, cycle_id, skip_job=None):
    """Planifie le step suivant.

    Idempotent : un cycle n'a jamais deux steps en attente. C'est ce qui permet
    de reprendre après une pause sans exécuter deux fois le même step.

    `skip_job` est le job en cours d'exécution. Il est déjà passé à « running »
    dans cette même transaction, donc sans cette exclusion il se compte lui-même
    comme travail en attente et le cycle s'arrête après la planification.
    """
    pending = q1(
        conn,
        """select id from jobs where cycle_id=%s and status in ('queued','running')
           and (%s::bigint is null or id <> %s::bigint) limit 1""",
        cycle_id,
        skip_job,
        skip_job,
    )
    if pending:
        return
    q(
        conn,
        "insert into jobs (project_id, cycle_id, kind) values (%s,%s,'run_step')",
        project_id,
        cycle_id,
    )


@app.post("/api/projects/{project_id}/cycles")
def start_cycle(project_id: str, body: CycleIn, me: dict = Depends(current_user)):
    with db() as conn:
        owned(conn, "projects", project_id, me)
        team = q(
            conn,
            """select pa.role as team_role, a.* from project_agents pa
               join agents a on a.id=pa.agent_id where pa.project_id=%s""",
            project_id,
        )
        if not team:
            raise HTTPException(400, "Le projet n'a aucun agent.")
        cycle = q1(
            conn,
            "insert into cycles (project_id, brief, status, demo) values (%s,%s,'queued',%s) returning *",
            project_id,
            body.brief,
            not llm_key_for(conn, me["id"]),
        )
        q(conn, "update projects set state='RUNNING' where id=%s", project_id)
        log(
            conn,
            project_id,
            cycle["id"],
            kind="system",
            title="Cycle démarré",
            body=body.brief or "Reprise de l'objectif permanent du projet.",
        )
        enqueue(conn, project_id, cycle["id"])
        return cycle


# --------------------------------------------------------------------------
# Routes — images d'un cycle
# --------------------------------------------------------------------------

# La limite de corps de requête Vercel est à 4,5 Mo ; on s'arrête avant, et on
# borne le nombre d'images pour que le coût d'un appel multimodal reste prévisible.
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGES_PER_CYCLE = 8


def _image_out(row: dict) -> dict:
    """Vue sortante : l'URL porte le jeton, seul moyen d'alimenter une balise <img>."""
    return {
        "id": str(row["id"]),
        "filename": row["filename"],
        "content_type": row["content_type"],
        "caption": row["caption"],
        "created_at": row["created_at"],
        "url": f"/api/images/{row['id']}?t={row['token']}",
    }


def _cycle_for_write(conn, cycle_id: str, me: dict) -> dict:
    cycle = q1(conn, "select * from cycles where id=%s", cycle_id)
    if not cycle:
        raise HTTPException(404, "Cycle introuvable.")
    owned(conn, "projects", cycle["project_id"], me)
    return cycle


@app.post("/api/cycles/{cycle_id}/images")
async def add_cycle_image(
    cycle_id: str,
    file: UploadFile = File(...),
    caption: str = Form(""),
    me: dict = Depends(current_user),
):
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "Seules des images peuvent être déposées ici.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Le fichier est vide.")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            400,
            f"Image trop lourde ({len(data) // 1024} Ko) : la limite est de "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} Mo.",
        )
    with db() as conn:
        cycle = _cycle_for_write(conn, cycle_id, me)
        count = q(conn, "select count(*) as n from cycle_images where cycle_id=%s", cycle_id)[0]["n"]
        if count >= MAX_IMAGES_PER_CYCLE:
            raise HTTPException(400, f"Ce cycle a déjà {MAX_IMAGES_PER_CYCLE} images.")
        row = q1(
            conn,
            """insert into cycle_images (project_id, cycle_id, filename, content_type, bytes, caption)
               values (%s,%s,%s,%s,%s,%s) returning *""",
            cycle["project_id"],
            cycle_id,
            (file.filename or "image")[:200],
            file.content_type,
            data,
            caption[:500],
        )
        log(conn, cycle["project_id"], cycle_id, kind="human", depth=1,
            title="Image jointe au cycle",
            body=caption or (file.filename or "image"))
        return _image_out(row)


@app.get("/api/cycles/{cycle_id}/images")
def list_cycle_images(cycle_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        cycle = q1(conn, "select * from cycles where id=%s", cycle_id)
        if not cycle:
            raise HTTPException(404, "Cycle introuvable.")
        readable(conn, "projects", cycle["project_id"], me)
        rows = q(
            conn,
            """select id, token, filename, content_type, caption, created_at
               from cycle_images where cycle_id=%s order by created_at""",
            cycle_id,
        )
        return [_image_out(r) for r in rows]


@app.get("/api/images/{image_id}")
def get_image(image_id: str, t: str = ""):
    """Sert les octets. Une balise <img> ne peut pas porter d'en-tête Authorization :
    l'accès tient donc au jeton aléatoire de la ligne, non devinable."""
    from fastapi import Response

    with db() as conn:
        row = q1(conn, "select * from cycle_images where id=%s", image_id)
        if not row or not t or str(row["token"]) != t:
            raise HTTPException(404, "Image introuvable.")
        return Response(
            content=bytes(row["bytes"]),
            media_type=row["content_type"],
            headers={"Cache-Control": "private, max-age=86400"},
        )


@app.delete("/api/images/{image_id}")
def delete_image(image_id: str, me: dict = Depends(current_user)):
    with db() as conn:
        row = q1(conn, "select project_id from cycle_images where id=%s", image_id)
        if not row:
            raise HTTPException(404, "Image introuvable.")
        owned(conn, "projects", row["project_id"], me)
        q(conn, "delete from cycle_images where id=%s", image_id)
    return {"ok": True}


@app.post("/api/tick")
def tick(me: dict | None = Depends(optional_user)):
    """Exécute exactement un step d'orchestration, puis rend la main.

    Un appelant ne fait jamais avancer que ses propres projets. Le cron quotidien
    de Vercel n'étant pas authentifié, il ne fait rien : l'avancement autonome
    reviendra avec un jeton de service dédié.
    """
    if me is None:
        return {"did": "unauthenticated"}
    with db() as conn:
        job = q1(
            conn,
            """select j.* from jobs j join projects p on p.id = j.project_id
               where j.status='queued' and j.run_after<=now() and p.owner_id=%s
               order by j.id for update of j skip locked limit 1""",
            me["id"],
        )
        if not job:
            return {"did": "nothing"}
        project = q1(conn, "select * from projects where id=%s", job["project_id"])
        if project and project["state"] == "PAUSED":
            return {"did": "paused"}
        q(conn, "update jobs set status='running', locked_at=now() where id=%s", job["id"])
        try:
            outcome = run_step(conn, job)
        except Exception as exc:  # pragma: no cover - robustesse serverless
            q(conn, "update jobs set status='failed', attempts=attempts+1 where id=%s", job["id"])
            log(conn, job["project_id"], job["cycle_id"], kind="system",
                title="Échec du step", body=str(exc)[:500])
            q(conn, "update cycles set status='failed', ended_at=now() where id=%s", job["cycle_id"])
            q(conn, "update projects set state='IDLE' where id=%s", job["project_id"])
            return {"did": "failed", "error": str(exc)[:500]}
        q(conn, "update jobs set status='done' where id=%s", job["id"])
        return {"did": outcome}


def team_of(conn, project_id) -> list[dict]:
    return q(
        conn,
        """select pa.role as team_role, a.* from project_agents pa
           join agents a on a.id=pa.agent_id where pa.project_id=%s
           order by case pa.role when 'orchestrator' then 0 when 'critic' then 2 else 1 end, a.name""",
        project_id,
    )


def memory_of(conn, agent_id) -> list[str]:
    rows = q(
        conn,
        "select content from agent_memory where agent_id=%s order by created_at desc limit 12",
        agent_id,
    )
    return [r["content"] for r in rows]


def run_step(conn, job) -> str:
    cycle = q1(conn, "select * from cycles where id=%s", job["cycle_id"])
    project = q1(conn, "select * from projects where id=%s", job["project_id"])
    team = team_of(conn, job["project_id"])
    if cycle["status"] == "queued":
        build_plan(conn, project, cycle, team)
        enqueue(conn, project["id"], cycle["id"], skip_job=job["id"])
        return "planned"
    if cycle["status"] != "running":
        return "idle"

    plan = cycle["plan"]
    cursor = cycle["cursor"]
    if cursor >= len(plan):
        q(conn, "update cycles set status='done', ended_at=now() where id=%s", cycle["id"])
        q(conn, "update projects set state='IDLE' where id=%s", project["id"])
        log(conn, project["id"], cycle["id"], kind="system", title="Cycle terminé",
            body=f"{len(plan)} steps exécutés.")
        return "done"

    step = plan[cursor]
    agent = next((a for a in team if str(a["id"]) == step["agent_id"]), None)
    if agent is None:
        q(conn, "update cycles set cursor=%s where id=%s", cursor + 1, cycle["id"])
        enqueue(conn, project["id"], cycle["id"], skip_job=job["id"])
        return "skipped"

    if step.get("gate"):
        # Le motif doit être lisible sans ouvrir le journal : qui bloque, sur quelle
        # action, et ce qu'on attend de l'humain.
        reason = {
            "why": step.get(
                "why",
                "Cette action est irréversible : elle ne part pas sans votre accord.",
            ),
            "agent": agent["name"],
            "action": step.get("label", "Validation requise"),
            "step_index": cursor + 1,
            "step_total": len(plan),
            "produced": q(
                conn,
                "select count(*) as n from artifacts where cycle_id=%s",
                cycle["id"],
            )[0]["n"],
        }
        # Quelles connexions cette action mobiliserait, et ce qui reste découvert.
        # Résolues chez le propriétaire du projet : le cycle avance aussi depuis
        # le cron, où l'appelant n'est pas forcément lui.
        required = step.get("required_capabilities", [])
        involved = connectors_covering(conn, project["owner_id"], required)
        covered = {c for row in involved for c in (row["capabilities"] or [])}
        q(
            conn,
            """insert into decisions (project_id, cycle_id, kind, title, detail, payload)
               values (%s,%s,'approval',%s,%s,%s)""",
            project["id"],
            cycle["id"],
            step.get("label", "Validation requise"),
            step.get("task", ""),
            json.dumps(
                {
                    "step": step,
                    "reason": reason,
                    "required_capabilities": required,
                    "connectors": [public_connector(r) for r in involved],
                    "missing": [c for c in required if c not in covered],
                },
                default=str,
            ),
        )
        q(conn, "update cycles set status='waiting_human' where id=%s", cycle["id"])
        log(conn, project["id"], cycle["id"], kind="gate", agent=agent,
            title="⏸ " + step.get("label", "Validation requise"),
            body="Le run est suspendu et son état conservé. Aucune ressource n'est occupée en attendant.")
        return "waiting_human"

    upstream = q(
        conn,
        "select agent_name, type, title, body from artifacts where cycle_id=%s order by created_at",
        cycle["id"],
    )
    log(conn, project["id"], cycle["id"], kind="call", agent=agent, depth=1,
        title=f"{agent['name']} · {step.get('label', 'travaille')}", body=step.get("task", ""))

    images = q(
        conn,
        """select content_type, bytes, caption from cycle_images
           where cycle_id=%s order by created_at limit %s""",
        cycle["id"],
        MAX_IMAGES_PER_CYCLE,
    )
    title, text, data = produce(
        project, cycle, agent, step, upstream, memory_of(conn, agent["id"]),
        images=images, api_key=llm_key_for(conn, project["owner_id"]),
    )

    q(
        conn,
        """insert into artifacts (project_id, cycle_id, agent_id, agent_name, type, title, body, data)
           values (%s,%s,%s,%s,%s,%s,%s,%s)""",
        project["id"],
        cycle["id"],
        agent["id"],
        agent["name"],
        step.get("kind", "content"),
        title,
        text,
        json.dumps(data),
    )
    log(conn, project["id"], cycle["id"], kind="result", agent=agent, depth=2,
        title=title, body=text, payload=data)

    next_cursor = cursor + 1
    # Boucle de révision bornée : le critique peut renvoyer le travail une fois.
    if step.get("kind") == "review" and data.get("verdict") == "revise":
        if cycle["revisions"] < project["max_revisions"]:
            target = next((i for i, s in enumerate(plan) if s.get("kind") == "content"), None)
            if target is not None:
                revision = dict(plan[target])
                revision["label"] = "révise après critique"
                revision["directive"] = data.get("request", "Reprendre les points soulevés.")
                plan = plan[: cursor + 1] + [revision] + plan[cursor + 1 :]
                q(conn, "update cycles set plan=%s, revisions=revisions+1 where id=%s",
                  json.dumps(plan), cycle["id"])
                log(conn, project["id"], cycle["id"], kind="system", depth=1,
                    title="Renvoi en révision",
                    body=f"Révision {cycle['revisions'] + 1} sur {project['max_revisions']} autorisées.")
        else:
            log(conn, project["id"], cycle["id"], kind="system", depth=1,
                title="Borne de révision atteinte",
                body="Le plafond de révisions est atteint, le cycle continue sans nouvelle boucle.")

    q(conn, "update cycles set cursor=%s, step_count=step_count+1 where id=%s", next_cursor, cycle["id"])
    if next_cursor < len(plan):
        enqueue(conn, project["id"], cycle["id"], skip_job=job["id"])
    else:
        q(conn, "update cycles set status='done', ended_at=now() where id=%s", cycle["id"])
        q(conn, "update projects set state='IDLE' where id=%s", project["id"])
        log(conn, project["id"], cycle["id"], kind="system", title="Cycle terminé",
            body=f"{next_cursor} steps exécutés.")
    return "step"


# --------------------------------------------------------------------------
# Planification et production
# --------------------------------------------------------------------------


def build_plan(conn, project, cycle, team) -> None:
    orchestrator = next((a for a in team if a["team_role"] == "orchestrator"), None)
    workers = [a for a in team if a["team_role"] == "specialist"]
    critics = [a for a in team if a["team_role"] == "critic"]
    brief = cycle["brief"] or project["objective"]

    plan: list[dict] = []
    for a in workers:
        plan.append(
            {
                "agent_id": str(a["id"]),
                "agent_name": a["name"],
                "label": "produit sa contribution",
                "kind": "content" if a is workers[-1] else "analysis",
                "task": f"{a['description'] or a['name']} — au service de : {brief}",
            }
        )
    for a in critics:
        plan.append(
            {
                "agent_id": str(a["id"]),
                "agent_name": a["name"],
                "label": "évalue la production",
                "kind": "review",
                "task": "Évaluer la qualité, la cohérence avec l'objectif et les points faibles.",
            }
        )
    if orchestrator:
        plan.append(
            {
                "agent_id": str(orchestrator["id"]),
                "agent_name": orchestrator["name"],
                "label": "Publication",
                "kind": "publish",
                "gate": True,
                "required_capabilities": PUBLISH_CAPABILITIES,
                "task": "Publier les contenus retenus. Action irréversible : validation humaine requise.",
                "why": "La publication est irréversible une fois partie. Le cycle s'arrête ici "
                       "et attend votre accord ; rien n'est publié tant que vous n'avez pas tranché.",
            }
        )

    q(conn, "update cycles set plan=%s, status='running', cursor=0 where id=%s",
      json.dumps(plan), cycle["id"])
    if orchestrator:
        log(conn, project["id"], cycle["id"], kind="call", agent=orchestrator, depth=0,
            title=f"{orchestrator['name']} établit le plan du cycle",
            body=brief)
    log(conn, project["id"], cycle["id"], kind="plan", depth=1,
        title=f"Plan arrêté : {len(plan)} steps",
        body=" → ".join(f"{s['agent_name']} ({s['label']})" for s in plan),
        payload={"plan": plan})


def produce(project, cycle, agent, step, upstream, memory, images=None, api_key="") -> tuple[str, str, dict]:
    """Produit la contribution d'un agent, en réel si possible, sinon en démo.

    Une clé présente mais un SDK absent ne doit pas faire échouer le cycle : on
    retombe sur le déroulé de démonstration en le disant explicitement.
    """
    images = images or []
    if not api_key:
        return produce_demo(project, cycle, agent, step, upstream, memory, images)
    try:
        return produce_live(project, cycle, agent, step, upstream, memory, images, api_key)
    except ImportError:
        title, text, data = produce_demo(project, cycle, agent, step, upstream, memory, images)
        data["sdk_missing"] = True
        return title, text + "\n\n(SDK openai-agents absent du déploiement : contenu de démonstration.)", data


def _context(project, cycle, step, upstream, memory, images=None) -> str:
    parts = [
        f"Objectif du projet : {project['objective'] or project['name']}",
        f"Consigne du cycle : {cycle['brief'] or 'appliquer l objectif permanent'}",
        f"Ta tâche : {step.get('task', '')}",
    ]
    if step.get("directive"):
        parts.append(f"Instruction humaine prioritaire : {step['directive']}")
    if memory:
        parts.append("Ta mémoire : " + " | ".join(memory))
    if images:
        parts.append(f"{len(images)} image(s) jointe(s) à ce cycle, à prendre en compte :")
        for i, img in enumerate(images, 1):
            parts.append(f"- image {i} : {img['caption'] or 'sans légende'}")
    if upstream:
        parts.append("Travaux déjà produits dans ce cycle :")
        for u in upstream[-4:]:
            parts.append(f"- [{u['agent_name']}] {u['title']} : {u['body'][:400]}")
    return "\n".join(parts)


# Borne de coût : au-delà, un cycle chargé en images ferait exploser la facture
# d'un seul appel sans rien apporter au raisonnement.
MAX_IMAGES_PER_CALL = 4


def produce_live(project, cycle, agent, step, upstream, memory, images=None, api_key="") -> tuple[str, str, dict]:
    import asyncio
    import base64

    from agents import Agent, Runner, set_default_openai_key

    # La clé vient du connecteur du propriétaire, ou du serveur : elle est résolue
    # par cycle, plus au chargement du module.
    set_default_openai_key(api_key)

    sdk_agent = Agent(
        name=agent["name"],
        instructions=(agent["instructions"] or agent["description"] or "Tu es un agent utile.")
        + "\n\nRéponds en français, de façon dense et concrète. Pas de préambule.",
        model=agent["model"] or "gpt-4.1-mini",
    )
    prompt = _context(project, cycle, step, upstream, memory, images)
    if step.get("kind") == "review":
        prompt += (
            "\n\nTermine impérativement ta réponse par une ligne seule : "
            "VERDICT: accept  ou  VERDICT: revise — suivie, si revise, de ce qu'il faut corriger."
        )

    if images:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for img in (images or [])[:MAX_IMAGES_PER_CALL]:
            b64 = base64.b64encode(bytes(img["bytes"])).decode()
            content.append({
                "type": "input_image",
                "detail": "auto",
                "image_url": f"data:{img['content_type']};base64,{b64}",
            })
        model_input: Any = [{"role": "user", "content": content}]
    else:
        model_input = prompt

    result = asyncio.run(Runner.run(sdk_agent, model_input, max_turns=4))
    text = str(result.final_output)
    data: dict[str, Any] = {"live": True}
    if step.get("kind") == "review":
        verdict = "revise" if "VERDICT: revise" in text else "accept"
        data["verdict"] = verdict
        data["request"] = text.split("VERDICT: revise", 1)[-1].strip()[:800] if verdict == "revise" else ""
    return f"{agent['name']} — {step.get('label', 'contribution')}", text, data


def produce_demo(project, cycle, agent, step, upstream, memory, images=None) -> tuple[str, str, dict]:
    """Déroulé de démonstration, sans appel de modèle.

    Le parcours, les écritures en base, la boucle de révision et la suspension
    pour validation sont réels ; seul le texte produit est fabriqué localement.
    """
    objective = project["objective"] or project["name"]
    kind = step.get("kind", "content")
    directive = step.get("directive", "")
    mem = f"\n\nCe que je retiens de ma mémoire : {memory[0]}" if memory else ""
    head = f"Instruction humaine prise en compte : {directive}\n\n" if directive else ""
    # Le mode démo n'analyse pas les images, mais il ne doit pas faire comme si
    # elles n'avaient pas été reçues.
    if images:
        legends = ", ".join(i["caption"] or "sans légende" for i in images)
        head = (
            f"{len(images)} image(s) reçue(s) ({legends}) — non analysées en mode démo, "
            "elles le seront dès qu'une clé de modèle sera disponible.\n\n"
        ) + head

    if kind == "analysis":
        body = (
            f"{head}En partant de « {objective} », je retiens trois angles exploitables.\n\n"
            f"1. Le premier angle s'appuie sur ce que {agent['name']} observe comme signal le plus "
            "constant : les audiences réagissent davantage à un cas concret qu'à une promesse "
            "générale.\n"
            "2. Le deuxième angle consiste à assumer une position tranchée plutôt qu'un panorama, "
            "parce qu'un panorama ne se retient pas.\n"
            "3. Le troisième angle exploite un écart entre ce que le secteur affirme et ce que les "
            "praticiens constatent.\n\n"
            f"Recommandation : traiter l'angle 2 en priorité.{mem}"
        )
        return (f"{agent['name']} — 3 angles retenus", body, {"demo": True, "angles": 3})

    if kind == "content":
        prior = upstream[-1]["title"] if upstream else "l'analyse amont"
        body = (
            f"{head}À partir de {prior}, voici la production.\n\n"
            "**Titre** — Ce que personne ne dit sur le sujet\n\n"
            f"La plupart des acteurs présentent « {objective} » comme un problème d'outillage. "
            "C'est un problème de décision. Tant que personne ne tranche sur ce qui compte, aucun "
            "outil ne rattrape le flou.\n\n"
            "Trois conséquences concrètes :\n"
            "— on empile des solutions qui ne se parlent pas ;\n"
            "— on mesure l'activité au lieu du résultat ;\n"
            "— on confond la vitesse avec l'avancement.\n\n"
            f"Ce qui marche : décider d'abord, outiller ensuite.{mem}"
        )
        return (f"{agent['name']} — brouillon", body, {"demo": True, "words": len(body.split())})

    if kind == "review":
        first_pass = cycle["revisions"] == 0
        if first_pass:
            body = (
                "Deux problèmes sérieux dans ce brouillon.\n\n"
                "**Le premier** : l'affirmation centrale n'est appuyée par aucun élément vérifiable. "
                "Telle quelle, elle se lit comme une opinion, et une opinion sans preuve ne convainc "
                "que ceux qui étaient déjà d'accord.\n\n"
                "**Le second** : la fin retombe sur une formule (« décider d'abord, outiller ensuite ») "
                "qui sonne bien mais n'indique aucune action. Le lecteur referme sans savoir quoi faire.\n\n"
                "Un point positif à conserver : le renversement de cadrage au début est efficace.\n\n"
                "VERDICT: revise"
            )
            return (
                f"{agent['name']} — 2 problèmes, renvoi en révision",
                body,
                {"demo": True, "verdict": "revise",
                 "request": "Étayer l'affirmation centrale et remplacer la conclusion par une action concrète."},
            )
        body = (
            "La révision corrige les deux points soulevés. L'affirmation centrale est maintenant "
            "appuyée, et la conclusion propose une action identifiable.\n\n"
            "Il reste une longueur excessive au troisième paragraphe, mais ce n'est pas bloquant.\n\n"
            "VERDICT: accept"
        )
        return (f"{agent['name']} — validé", body, {"demo": True, "verdict": "accept"})

    return (f"{agent['name']} — note", f"{head}Contribution enregistrée.{mem}", {"demo": True})


# --------------------------------------------------------------------------
# Amorçage : une équipe et un projet prêts à l'emploi
# --------------------------------------------------------------------------

SEED_AGENTS = [
    ("Chef d'orchestre", "orchestrator",
     "Coordonne l'équipe, arrête le plan du cycle, arbitre et décide de ce qui part en publication.",
     "Tu diriges une équipe d'agents. Tu ne rédiges pas toi-même : tu décides quoi demander, à qui, "
     "dans quel ordre, et tu tranches. Tu préfères une décision nette à un consensus mou.", "violet"),
    ("Veille", "specialist",
     "Collecte et trie les signaux pertinents pour l'objectif du projet.",
     "Tu cherches des signaux concrets et récents. Tu écartes tout ce qui est générique. Pour chaque "
     "signal, tu indiques pourquoi il est pertinent maintenant.", "sky"),
    ("Stratège", "specialist",
     "Transforme les signaux en angles exploitables et les priorise.",
     "Tu convertis des observations en angles actionnables. Tu en proposes trois au maximum, "
     "classés, avec le motif du classement. Tu assumes de recommander un seul angle.", "amber"),
    ("Rédacteur", "specialist",
     "Produit le contenu à partir de l'angle retenu.",
     "Tu écris de façon dense et directe. Pas de préambule, pas de formule creuse. Une idée par "
     "paragraphe. Tu préfères un exemple concret à une généralité.", "emerald"),
    ("Critique", "critic",
     "Évalue la production, identifie les faiblesses et renvoie en révision si nécessaire.",
     "Tu es exigeant et précis. Tu identifies ce qui ne tient pas et tu expliques pourquoi. Tu "
     "signales aussi ce qui fonctionne. Tu conclus par VERDICT: accept ou VERDICT: revise.", "rose"),
]


@app.post("/api/seed")
def seed(me: dict = Depends(current_user)):
    with db() as conn:
        existing = q(
            conn, f"select count(*) as n from agents where {MINE}", me["id"]
        )[0]["n"]
        if existing:
            project = q1(
                conn,
                f"select id from projects where {MINE} order by created_at limit 1",
                me["id"],
            )
            return {"ok": True, "skipped": True,
                    "project_id": str(project["id"]) if project else None}
        created = []
        for name, role, description, instructions, accent in SEED_AGENTS:
            created.append(
                q1(
                    conn,
                    """insert into agents (owner_id, name, description, role, instructions, accent)
                       values (%s,%s,%s,%s,%s,%s) returning *""",
                    me["id"],
                    name,
                    description,
                    role,
                    instructions,
                    accent,
                )
            )
        project = q1(
            conn,
            """insert into projects (owner_id, name, objective)
               values (%s,%s,%s) returning *""",
            me["id"],
            "Contenu organique B2B",
            "Générer des leads B2B qualifiés grâce à du contenu organique, sans publicité.",
        )
        positions = [(400, 60), (140, 240), (330, 240), (520, 240), (400, 420)]
        for agent, (x, y) in zip(created, positions):
            role = "orchestrator" if agent["role"] == "orchestrator" else (
                "critic" if agent["role"] == "critic" else "specialist"
            )
            q(
                conn,
                "insert into project_agents (project_id, agent_id, role, x, y) values (%s,%s,%s,%s,%s)",
                project["id"],
                agent["id"],
                role,
                x,
                y,
            )
        boss = created[0]
        for other in created[1:]:
            q(
                conn,
                """insert into architecture_edges (project_id, source_agent_id, target_agent_id, kind)
                   values (%s,%s,%s,%s) on conflict do nothing""",
                project["id"],
                boss["id"],
                other["id"],
                "reviews" if other["role"] == "critic" else "calls",
            )
        q(
            conn,
            "insert into agent_memory (agent_id, content) values (%s,%s)",
            created[3]["id"],
            "L'audience réagit mieux aux formats courts avec un exemple chiffré qu'aux tribunes longues.",
        )
        return {"ok": True, "project_id": str(project["id"])}
