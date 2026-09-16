# Agent Studio — Architecture technique du MVP

Agent Studio est une application qui permet de créer des agents IA, de les réunir en projets, de définir comment ils se parlent, et de les laisser produire du contenu de façon autonome sous le contrôle d'un humain qui peut à tout moment mettre en pause, arrêter, intervenir ou arbitrer.

Ce document décrit l'architecture exacte visée par le MVP : quels processus tournent, comment les messages circulent entre agents, comment les réveils sont programmés, comment l'état est stocké, et comment fonctionnent les commandes Pause / Reprendre / Stop / Intervenir.

## 1. Décisions structurantes

Trois décisions déterminent tout le reste.

**Le Manager n'est pas un processus vivant.** Il n'existe pas de démon « Manager » qui garde une conversation en mémoire pendant des heures. Le Manager est reconstruit à chaque tour d'orchestration à partir de l'état stocké en base, il agit, il persiste son nouvel état, puis il disparaît. On peut redémarrer n'importe quelle machine à n'importe quel moment sans rien perdre. C'est la condition pour que Pause, Stop et Intervenir soient fiables plutôt qu'approximatifs.

**L'API ne fait jamais tourner d'agent.** Une requête HTTP ne doit pas durer dix minutes. Tout appel de modèle a lieu dans un worker, découplé de l'API par une file de jobs. L'API lit et écrit la base, et pousse des événements vers l'interface.

**Le niveau d'autonomie est décidé côté serveur, jamais par le modèle.** Le LLM propose une action, une politique déterministe stockée en base décide si elle part directement ou si elle attend un humain. Un modèle qui déciderait lui-même s'il a besoin d'autorisation n'est pas un garde-fou.

## 2. Les processus

Quatre processus, aucune infrastructure au-delà de Postgres.

    ┌───────────────┐   ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
    │  web          │   │  api          │   │  scheduler    │   │  worker × N   │
    │  React + Vite │   │  FastAPI      │   │  tick 60 s    │   │  asyncio      │
    │  :5173        │   │  :8000        │   │  leader unique│   │  exécute      │
    │               │   │  REST + SSE   │   │               │   │  les agents   │
    └───────┬───────┘   └───────┬───────┘   └───────┬───────┘   └───────┬───────┘
            │ HTTP + SSE        │                   │                   │
            └───────────────────┤                   │                   │
                                ▼                   ▼                   ▼
                    ┌───────────────────────────────────────────────────────┐
                    │                   PostgreSQL                          │
                    │  état durable · file de jobs · journal · LISTEN/NOTIFY │
                    └───────────────────────────────────────────────────────┘

`api` expose le CRUD des agents, des projets et des missions, les commandes de contrôle, et un flux SSE par projet. Il ne charge jamais le SDK des agents.

`scheduler` est un processus unique qui, toutes les soixante secondes, cherche les missions dont l'heure de réveil est passée et insère un job. L'unicité est garantie par un advisory lock Postgres, donc deux instances lancées par erreur ne produisent pas deux cycles.

`worker` consomme la file avec `SELECT ... FOR UPDATE SKIP LOCKED`, ce qui permet de lancer plusieurs workers sans coordination externe. Un worker traite un tour d'orchestration, persiste le résultat, et libère le job.

Postgres sert de file, de verrou et de bus temps réel via `LISTEN/NOTIFY`, que l'API relaie en SSE. Pas de Redis, pas de Celery, pas de RabbitMQ au MVP : ils ajouteraient trois modes de panne pour un gain nul à cette échelle.

## 3. Les cinq unités de temps

C'est la hiérarchie qui rend le contrôle humain possible.

    mission          objectif permanent, ex. « générer des leads B2B »
     └── cycle       un réveil planifié, ex. le cycle du 16/09 à 06:00
          └── step   un tour d'orchestration = un job = une transaction
               └── run    un appel Runner.run() du SDK, interruptible
                    └── turn   un aller-retour avec le modèle

Un `step` est court et reprenable. Entre deux steps, rien ne tourne : tout est en base. Un `run` peut en revanche s'interrompre au milieu, quand un outil demande une approbation humaine ; son état est alors sérialisé et le worker est libéré immédiatement. Un run peut rester en attente trois jours sans occuper la moindre ressource.

## 4. Le modèle de données

Les tables se répartissent en cinq familles.

**Définition des agents.** `agents` porte l'identité (nom, description, propriétaire). `agent_versions` porte la configuration versionnée : instructions, modèle, `model_settings`, outils activés, type de sortie structurée. Une version est immuable une fois utilisée dans un run, ce qui permet de dire plus tard qu'un contenu a été produit par « Rédacteur v3 » et de comparer des versions de prompt sur des métriques réelles.

**Composition des projets.** `projects` décrit l'objectif. `project_agents` associe un agent à un projet avec un rôle (`orchestrator`, `specialist`, `critic`). `architecture_edges` décrit qui peut parler à qui, avec un type d'arête (`calls`, `reviews`, `handoff`) : c'est la structure que l'interface dessine et que le runtime applique littéralement en construisant la liste d'outils de chaque agent.

**Exécution.** `missions` porte l'objectif permanent, la planification cron, l'état (`RUNNING`, `PAUSED`, `STOPPED`) et les budgets. `cycles`, `steps` et `jobs` matérialisent la hiérarchie ci-dessus. `runs` stocke le `RunState` sérialisé du SDK, c'est-à-dire la reprise possible.

**Contenu et mémoire.** `messages` est un journal append-only de tout ce qui s'est passé, destiné à l'affichage et à l'audit, jamais à la décision. `artifacts` contient les objets structurés produits par les agents (opportunité, brouillon, critique), typés et requêtables. `facts` est la mémoire durable du projet : identité de marque, audience, décisions passées, enseignements. `agent_sessions` relie chaque agent à une session du SDK pour sa mémoire conversationnelle.

**Contrôle.** `policies` définit, par action, le niveau d'autonomie requis. `decisions` est la boîte de réception humaine : chaque entrée référence un run interrompu et attend une approbation, un rejet ou une instruction. `usage` enregistre tokens et coût par step, pour que les budgets soient vérifiés avant l'appel et non constatés après.

## 5. Comment les agents se parlent

Trois canaux distincts, et surtout pas une boîte aux lettres généralisée entre agents.

**L'appel.** Le Manager appelle un spécialiste via `agent.as_tool()`. C'est synchrone à l'intérieur du run, le Manager garde la vue d'ensemble, et le retour est un objet Pydantic typé, pas du texte libre. La liste des outils d'un agent est construite au moment de l'exécution à partir de `architecture_edges` : l'architecture dessinée dans l'interface est exactement celle qui s'exécute.

**Le journal.** Des `RunHooks` du SDK écrivent chaque début et fin d'appel dans `messages`, avec la profondeur d'imbrication. C'est ce que l'interface affiche en direct. Aucun agent ne lit le journal : c'est une trace, pas un canal.

**L'artefact.** Quand un résultat doit survivre au run qui l'a produit — une opportunité détectée lundi et exploitée mardi — il est écrit dans `artifacts` avec son type et son schéma. C'est le seul mode de communication entre deux cycles.

Les échanges sont structurés parce que du texte libre entre agents rend le système impossible à superviser. Un spécialiste ne renvoie pas « je pense que cette tendance est intéressante » mais un objet avec ses preuves, son audience cible, son potentiel estimé et son action recommandée.

## 6. Comment les réveils sont programmés

Trois sources de réveil, une seule file.

Le cron, porté par `missions.schedule` et matérialisé par `missions.next_run_at`. Le scheduler insère un job avec une clé d'idempotence unique sur `(mission_id, scheduled_for)`, ce qui rend le double déclenchement structurellement impossible.

L'enchaînement, quand un step se termine et planifie le suivant dans la même transaction que l'écriture de son résultat. C'est ce qui fait avancer un cycle sans processus long.

L'humain, quand il lance un cycle à la main ou répond à une décision en attente, ce qui insère un job de reprise.

Un job porte un type (`start_cycle`, `continue_run`, `resume_after_decision`), une référence, un nombre de tentatives et une date de disponibilité. L'échec transitoire est réessayé avec un délai croissant ; l'échec définitif ouvre une entrée dans `decisions` plutôt que de disparaître dans des logs.

## 7. Pause, Reprendre, Stop, Intervenir

Ces quatre commandes s'appuient directement sur les primitives du SDK présentes dans ce dépôt : `RunState`, les interruptions d'outils, `approve`, `reject` et `add_input`.

**Pause** passe la mission à `PAUSED`. Le scheduler cesse de produire des jobs. Le worker termine proprement le step en cours puis ne planifie pas le suivant. Aucun appel en vol n'est coupé, donc aucun contenu à moitié produit.

**Reprendre** repasse la mission à `RUNNING` et insère un job de continuation. Le worker recharge le dernier `RunState` sérialisé et appelle `Runner.run(agent, state)`. La conversation reprend exactement où elle s'était arrêtée.

**Stop** passe la mission à `STOPPED` et émet un `NOTIFY` sur le canal d'annulation du run. Le worker annule la tâche asyncio en cours. L'état sérialisé reste en base : un arrêt n'est pas une destruction, on peut consulter ce qui avait été fait et reprendre plus tard.

**Intervenir** injecte une instruction humaine dans la conversation du Manager via `RunState.add_input()`, puis relance. L'instruction « ne cible plus les PME, cible les agences » est prise en compte au tour suivant, sans perdre le contexte accumulé.

**Arbitrer** répond à une interruption d'approbation. Quand un outil protégé est appelé, le run s'interrompt, le worker écrit `result.to_state().to_json()` dans `runs`, crée une entrée dans `decisions` et se libère. L'humain approuve ou rejette depuis l'interface ; la réponse déclenche `state.approve(item)` ou `state.reject(item)` suivi d'une reprise.

La protection d'un outil est déclarée par un `needs_approval` qui interroge la table `policies` :

    async def gate(ctx, params, call_id) -> bool:
        return await policies.requires_human(
            project_id=ctx.context.project_id,
            action=params["action"],
            estimated_cost_eur=params.get("cost_eur", 0.0),
        )

## 8. Les outils du Manager

Le Manager dispose de deux familles d'outils, et la distinction compte.

Les outils d'agent sont produits par `agent.as_tool()` à partir de l'architecture du projet : interroger la veille, demander une analyse stratégique, commander des contenus, faire critiquer une production. Leur signature d'entrée et leur type de sortie sont des modèles Pydantic.

Les outils système écrivent dans la base : consulter la mémoire du projet, enregistrer une décision, créer une tâche, demander explicitement un arbitrage humain, proposer l'ajout d'un agent existant au projet. Ce dernier point mérite une précision : l'outil de proposition ne modifie jamais l'équipe, il crée une proposition en attente que l'humain accepte ou refuse. Un système d'agents qui peut se recomposer lui-même sans validation est ingérable.

## 9. Garde-fous

Quatre règles déterminent quand un humain est sollicité, et aucune ne repose sur l'auto-évaluation du modèle.

Le coût et l'irréversibilité : publier, dépenser, envoyer, supprimer passent par une validation ou une configuration explicite. Lire, analyser, générer et critiquer sont automatiques.

La nouveauté : une action jamais effectuée dans ce projet demande une validation la première fois, puis peut être autorisée durablement.

Le désaccord : quand deux agents produisent des conclusions incompatibles, c'est un signal exploitable, enregistré comme artefact de type désaccord et escaladé, pas moyenné en silence.

La borne de révision : la boucle critique vers rédaction est limitée à deux passes, après quoi le cas est escaladé. Sans cette borne, un critique exigeant et un rédacteur docile peuvent consommer un budget entier sur un seul contenu.

Le seuil de confiance auto-déclarée par le modèle est délibérément écarté : un LLM à qui on demande sa confiance apprend vite à répondre le nombre qui fait passer l'action.

## 10. Interface

Quatre écrans, sobres, denses, une seule couleur d'accent.

La bibliothèque d'agents, avec l'éditeur de version : nom, description, instructions, modèle, outils, mémoire.

Le projet, avec le canevas d'architecture en SVG : les agents sont des nœuds, les arêtes disent qui parle à qui, et modifier le canevas modifie réellement la composition des outils à l'exécution.

Le direct, qui affiche le journal du cycle en cours en SSE, avec l'imbrication des appels visible.

La boîte de réception, qui liste les décisions en attente avec le contexte nécessaire pour trancher sans aller chercher ailleurs. C'est l'écran qui détermine si le système est utilisable au quotidien.

## 11. Ce que le MVP ne fait pas

La mémoire sémantique vectorielle est repoussée : `facts` et une recherche plein texte Postgres couvrent le besoin initial, et pgvector s'ajoutera sans migration structurelle.

La publication réelle sur des plateformes externes est repoussée : le MVP produit et planifie, la distribution viendra par MCP une fois le noyau stable.

Le multi-utilisateur et les permissions fines sont repoussés : un propriétaire par projet suffit pour valider l'architecture.
