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

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

# Rempli au déploiement. En production, préférer la variable d'environnement.
_FALLBACK_DSN = ""

DATABASE_URL = os.environ.get("DATABASE_URL") or _FALLBACK_DSN
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LIVE = bool(OPENAI_API_KEY)

app = FastAPI(title="Agent Studio")


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


class ProjectIn(BaseModel):
    name: str
    objective: str = ""
    max_revisions: int = 2


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


class MemoryIn(BaseModel):
    content: str


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
def state():
    ok, detail = True, "connectée"
    try:
        with db() as conn:
            q1(conn, "select 1 as ok")
    except Exception as exc:  # pragma: no cover - dépend de l'infra
        ok, detail = False, str(exc)[:200]
    return {"db": ok, "db_detail": detail, "live": LIVE, "mode": "live" if LIVE else "demo"}


@app.get("/api/summary")
def summary():
    """Compteurs globaux affichés en permanence dans la barre latérale."""
    with db() as conn:
        row = q(
            conn,
            """select
                 count(*) filter (where state='RUNNING') as running,
                 count(*) filter (where state='PAUSED')  as paused
               from projects""",
        )[0]
        pending = q(
            conn, "select count(*) as n from decisions where status='pending'"
        )[0]["n"]
        return {"running": row["running"], "paused": row["paused"], "pending": pending}


@app.get("/api/agents")
def list_agents():
    with db() as conn:
        return q(conn, "select * from agents order by created_at")


@app.post("/api/agents")
def create_agent(body: AgentIn):
    with db() as conn:
        return q1(
            conn,
            """insert into agents (name, description, role, instructions, model, temperature, accent)
               values (%s,%s,%s,%s,%s,%s,%s) returning *""",
            body.name,
            body.description,
            body.role,
            body.instructions,
            body.model,
            body.temperature,
            body.accent,
        )


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: str, body: AgentIn):
    with db() as conn:
        row = q1(
            conn,
            """update agents set name=%s, description=%s, role=%s, instructions=%s,
                                 model=%s, temperature=%s, accent=%s, updated_at=now()
               where id=%s returning *""",
            body.name,
            body.description,
            body.role,
            body.instructions,
            body.model,
            body.temperature,
            body.accent,
            agent_id,
        )
        if not row:
            raise HTTPException(404, "agent introuvable")
        return row


@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str):
    with db() as conn:
        q(conn, "delete from agents where id=%s", agent_id)
    return {"ok": True}


@app.get("/api/agents/{agent_id}/memory")
def get_memory(agent_id: str):
    with db() as conn:
        return q(conn, "select * from agent_memory where agent_id=%s order by created_at desc", agent_id)


@app.post("/api/agents/{agent_id}/memory")
def add_memory(agent_id: str, body: MemoryIn):
    with db() as conn:
        return q1(
            conn,
            "insert into agent_memory (agent_id, content) values (%s,%s) returning *",
            agent_id,
            body.content,
        )


@app.delete("/api/memory/{memory_id}")
def del_memory(memory_id: str):
    with db() as conn:
        q(conn, "delete from agent_memory where id=%s", memory_id)
    return {"ok": True}


# --------------------------------------------------------------------------
# Routes — projets
# --------------------------------------------------------------------------


@app.get("/api/projects")
def list_projects():
    with db() as conn:
        return q(
            conn,
            """select p.*, (select count(*) from project_agents pa where pa.project_id=p.id) as team_size,
                      (select count(*) from artifacts a where a.project_id=p.id) as artifact_count,
                      (select count(*) from decisions d where d.project_id=p.id and d.status='pending')
                        as pending_count
               from projects p order by p.created_at desc""",
        )


@app.post("/api/projects")
def create_project(body: ProjectIn):
    with db() as conn:
        return q1(
            conn,
            "insert into projects (name, objective, max_revisions) values (%s,%s,%s) returning *",
            body.name,
            body.objective,
            body.max_revisions,
        )


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    with db() as conn:
        q(conn, "delete from projects where id=%s", project_id)
    return {"ok": True}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    with db() as conn:
        project = q1(conn, "select * from projects where id=%s", project_id)
        if not project:
            raise HTTPException(404, "projet introuvable")
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
def set_team(project_id: str, body: list[TeamMember]):
    with db() as conn:
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
def set_edges(project_id: str, body: list[Edge]):
    with db() as conn:
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
def suggestions(project_id: str):
    """Agents existants que l'application propose d'ajouter au projet.

    L'application propose, elle ne recompose jamais l'équipe toute seule.
    """
    with db() as conn:
        project = q1(conn, "select * from projects where id=%s", project_id)
        if not project:
            raise HTTPException(404, "projet introuvable")
        candidates = q(
            conn,
            """select * from agents where id not in
                 (select agent_id from project_agents where project_id=%s)""",
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
def stream(project_id: str, after: int = 0):
    with db() as conn:
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


@app.post("/api/decisions/{decision_id}/respond")
def respond(decision_id: str, body: DecisionIn):
    with db() as conn:
        d = q1(conn, "select * from decisions where id=%s", decision_id)
        if not d:
            raise HTTPException(404, "décision introuvable")
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
def control(project_id: str, body: ControlIn):
    action = body.action
    with db() as conn:
        project = q1(conn, "select * from projects where id=%s", project_id)
        if not project:
            raise HTTPException(404, "projet introuvable")
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
# Cycle : création et exécution pas à pas
# --------------------------------------------------------------------------


def enqueue(conn, project_id, cycle_id):
    """Planifie le step suivant.

    Idempotent : un cycle n'a jamais deux steps en attente. C'est ce qui permet
    de reprendre après une pause sans exécuter deux fois le même step.
    """
    pending = q1(
        conn,
        "select id from jobs where cycle_id=%s and status in ('queued','running') limit 1",
        cycle_id,
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
def start_cycle(project_id: str, body: CycleIn):
    with db() as conn:
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
            not LIVE,
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


@app.post("/api/tick")
def tick():
    """Exécute exactement un step d'orchestration, puis rend la main."""
    with db() as conn:
        job = q1(
            conn,
            """select * from jobs where status='queued' and run_after<=now()
               order by id for update skip locked limit 1""",
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
        enqueue(conn, project["id"], cycle["id"])
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
        enqueue(conn, project["id"], cycle["id"])
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
        q(
            conn,
            """insert into decisions (project_id, cycle_id, kind, title, detail, payload)
               values (%s,%s,'approval',%s,%s,%s)""",
            project["id"],
            cycle["id"],
            step.get("label", "Validation requise"),
            step.get("task", ""),
            json.dumps({"step": step, "reason": reason}),
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

    title, text, data = produce(project, cycle, agent, step, upstream, memory_of(conn, agent["id"]))

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
        enqueue(conn, project["id"], cycle["id"])
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


def produce(project, cycle, agent, step, upstream, memory) -> tuple[str, str, dict]:
    """Produit la contribution d'un agent, en réel si possible, sinon en démo.

    Une clé présente mais un SDK absent ne doit pas faire échouer le cycle : on
    retombe sur le déroulé de démonstration en le disant explicitement.
    """
    if not LIVE:
        return produce_demo(project, cycle, agent, step, upstream, memory)
    try:
        return produce_live(project, cycle, agent, step, upstream, memory)
    except ImportError:
        title, text, data = produce_demo(project, cycle, agent, step, upstream, memory)
        data["sdk_missing"] = True
        return title, text + "\n\n(SDK openai-agents absent du déploiement : contenu de démonstration.)", data


def _context(project, cycle, step, upstream, memory) -> str:
    parts = [
        f"Objectif du projet : {project['objective'] or project['name']}",
        f"Consigne du cycle : {cycle['brief'] or 'appliquer l objectif permanent'}",
        f"Ta tâche : {step.get('task', '')}",
    ]
    if step.get("directive"):
        parts.append(f"Instruction humaine prioritaire : {step['directive']}")
    if memory:
        parts.append("Ta mémoire : " + " | ".join(memory))
    if upstream:
        parts.append("Travaux déjà produits dans ce cycle :")
        for u in upstream[-4:]:
            parts.append(f"- [{u['agent_name']}] {u['title']} : {u['body'][:400]}")
    return "\n".join(parts)


def produce_live(project, cycle, agent, step, upstream, memory) -> tuple[str, str, dict]:
    import asyncio

    from agents import Agent, Runner

    sdk_agent = Agent(
        name=agent["name"],
        instructions=(agent["instructions"] or agent["description"] or "Tu es un agent utile.")
        + "\n\nRéponds en français, de façon dense et concrète. Pas de préambule.",
        model=agent["model"] or "gpt-4.1-mini",
    )
    prompt = _context(project, cycle, step, upstream, memory)
    if step.get("kind") == "review":
        prompt += (
            "\n\nTermine impérativement ta réponse par une ligne seule : "
            "VERDICT: accept  ou  VERDICT: revise — suivie, si revise, de ce qu'il faut corriger."
        )
    result = asyncio.run(Runner.run(sdk_agent, prompt, max_turns=4))
    text = str(result.final_output)
    data: dict[str, Any] = {"live": True}
    if step.get("kind") == "review":
        verdict = "revise" if "VERDICT: revise" in text else "accept"
        data["verdict"] = verdict
        data["request"] = text.split("VERDICT: revise", 1)[-1].strip()[:800] if verdict == "revise" else ""
    return f"{agent['name']} — {step.get('label', 'contribution')}", text, data


def produce_demo(project, cycle, agent, step, upstream, memory) -> tuple[str, str, dict]:
    """Déroulé de démonstration, sans appel de modèle.

    Le parcours, les écritures en base, la boucle de révision et la suspension
    pour validation sont réels ; seul le texte produit est fabriqué localement.
    """
    objective = project["objective"] or project["name"]
    kind = step.get("kind", "content")
    directive = step.get("directive", "")
    mem = f"\n\nCe que je retiens de ma mémoire : {memory[0]}" if memory else ""
    head = f"Instruction humaine prise en compte : {directive}\n\n" if directive else ""

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
def seed():
    with db() as conn:
        existing = q(conn, "select count(*) as n from agents")[0]["n"]
        if existing:
            return {"ok": True, "skipped": True}
        created = []
        for name, role, description, instructions, accent in SEED_AGENTS:
            created.append(
                q1(
                    conn,
                    """insert into agents (name, description, role, instructions, accent)
                       values (%s,%s,%s,%s,%s) returning *""",
                    name,
                    description,
                    role,
                    instructions,
                    accent,
                )
            )
        project = q1(
            conn,
            """insert into projects (name, objective) values (%s,%s) returning *""",
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
