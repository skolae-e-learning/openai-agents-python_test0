# Construire Agent Studio, une application de production de contenu par équipes d'agents IA

Ce document est vivant. Les sections Avancement, Surprises et découvertes, Journal des décisions et Bilan doivent rester à jour au fil du travail. Il est maintenu conformément à `PLANS.md` à la racine du dépôt. L'architecture visée est décrite dans `agent-studio/ARCHITECTURE.md`.

## Objectif et vision d'ensemble

À la fin de ce plan, un utilisateur peut ouvrir Agent Studio dans son navigateur, créer plusieurs agents IA ayant chacun son nom, sa description, ses instructions, son modèle et sa mémoire, les réunir dans un projet, dessiner l'architecture qui dit lequel parle à lequel, définir une mission permanente, et laisser le système produire du contenu tout seul selon une planification. Il voit le déroulement en direct, il reçoit dans une boîte de réception les décisions qui demandent son arbitrage, et il dispose à tout moment de quatre commandes : mettre en pause, reprendre, arrêter, et injecter une instruction dans la réflexion en cours.

On observe le résultat en lançant `make dev`, en ouvrant `http://localhost:5173`, en créant une équipe de cinq agents, en démarrant une mission, et en constatant que le journal se remplit sans intervention puis s'arrête sur une demande de validation.

## Vocabulaire

Un **agent** est une configuration versionnée passée au SDK `openai-agents` présent dans ce dépôt sous `src/agents/` : un nom, des instructions, un modèle, des outils et un type de sortie. Un **projet** réunit des agents et l'architecture qui les relie. Une **mission** est un objectif permanent attaché à un projet, avec une planification. Un **cycle** est un réveil de la mission. Un **step** est un tour d'orchestration, c'est-à-dire un job traité par un worker dans une transaction. Un **run** est un appel `Runner.run()` du SDK, qui peut s'interrompre quand un outil demande une approbation. Le **RunState** est l'objet du SDK, sérialisable en JSON, qui permet de reprendre un run interrompu ; il est défini dans `src/agents/run_state.py` et illustré par `examples/agent_patterns/human_in_the_loop.py`.

## Avancement

- [ ] M0 — Fondations : docker compose, migrations, file de jobs, quatre processus qui démarrent.
- [ ] M1 — Agents et projets : CRUD complet et interface React.
- [ ] M2 — Premier run : exécuter un agent seul et voir son journal en direct.
- [ ] M3 — L'équipe : Manager, outils d'agents, contrats structurés.
- [ ] M4 — L'humain dans la boucle : approbations, RunState persisté, boîte de réception.
- [ ] M5 — Le contrôle : pause, reprise, arrêt, intervention, planification cron.
- [ ] M6 — Les garde-fous : politiques d'autonomie, budgets, canevas d'architecture éditable.

## M0 — Fondations

L'objectif est d'avoir un squelette qui démarre et qui fait circuler un job de bout en bout, sans aucun agent. On crée `agent-studio/` avec un `pyproject.toml` qui dépend du SDK local, un `docker-compose.yml` fournissant Postgres, des migrations Alembic pour les tables décrites dans l'architecture, et trois entrées `api`, `scheduler` et `worker`. On ajoute un job factice qui écrit une ligne dans `messages`, et un flux SSE qui la fait apparaître.

La preuve est un test d'intégration qui insère un job, laisse le worker le consommer, et vérifie que la ligne attendue apparaît dans `messages` et sur le flux SSE. On vérifie aussi que deux workers lancés en parallèle ne traitent jamais le même job, en s'appuyant sur `FOR UPDATE SKIP LOCKED`.

## M1 — Agents et projets

L'objectif est que l'utilisateur puisse créer ce dont il a besoin avant qu'un seul agent ne tourne. On expose le CRUD des agents et de leurs versions, des projets, de l'association agent-projet et des arêtes d'architecture. Côté React, on livre la coquille de l'application, la bibliothèque d'agents et l'éditeur de version, dans un style sobre et dense.

La preuve est de créer cinq agents, de les réunir dans un projet, de relier le Manager aux quatre autres, de recharger la page et de retrouver exactement la même chose.

## M2 — Premier run

L'objectif est de faire tourner un agent unique et de le regarder travailler. Le worker apprend à construire un `Agent` du SDK à partir d'une version stockée, à l'exécuter avec `Runner.run()`, et à écrire le journal via des `RunHooks`. La session de mémoire conversationnelle de l'agent est branchée sur une session du SDK. L'écran Direct affiche le flux.

La preuve est de lancer un agent rédacteur sur une consigne, de voir les messages arriver en direct, et de retrouver le contenu produit dans `artifacts`. Un test utilise `ScriptedModel` de `agents.testing` pour valider le déroulement sans appeler le modèle réel.

## M3 — L'équipe

L'objectif est que les agents se parlent. Le worker construit les outils du Manager à partir des arêtes d'architecture, en utilisant `agent.as_tool()` avec des schémas d'entrée et des types de sortie Pydantic. On introduit les cinq rôles de départ : Manager, Veille, Stratège, Rédacteur, Critique. Les échanges produisent des artefacts typés plutôt que du texte libre. La boucle critique vers rédaction est bornée à deux passes.

La preuve est un cycle complet où le Manager interroge la veille, fait analyser les résultats, commande des contenus, les fait critiquer et renvoie en révision ce qui doit l'être, le tout visible dans le journal avec l'imbrication des appels.

## M4 — L'humain dans la boucle

L'objectif est qu'un run puisse s'arrêter pour demander l'avis de l'utilisateur sans bloquer de ressource. Certains outils reçoivent un `needs_approval`. Quand le run s'interrompt, le worker sérialise `result.to_state().to_json()` dans `runs`, ouvre une entrée dans `decisions`, notifie l'interface et se libère. L'écran Boîte de réception présente le contexte et les boutons d'approbation. La réponse déclenche `approve` ou `reject` puis une reprise.

La preuve est d'arrêter complètement les workers pendant qu'une décision est en attente, de les redémarrer, de répondre depuis l'interface, et de constater que le run reprend exactement où il s'était arrêté.

## M5 — Le contrôle

L'objectif est de livrer les quatre commandes. Pause arrête la production de jobs sans couper l'appel en cours. Reprendre réinjecte un job de continuation. Arrêter émet une annulation vers le worker via `NOTIFY` tout en conservant l'état sérialisé. Intervenir appelle `RunState.add_input()` avec l'instruction de l'utilisateur avant la reprise. Le scheduler prend en charge la planification cron des missions, avec une clé d'idempotence qui interdit le double déclenchement.

La preuve est un scénario où l'on met en pause en plein cycle, où l'on injecte un changement de cible, où l'on reprend, et où l'on constate que la suite du cycle tient compte de l'instruction.

## M6 — Les garde-fous

L'objectif est que l'autonomie soit paramétrable et bornée. Les politiques par action sont stockées et consultées par les fonctions d'approbation. Les budgets en euros et en tokens sont vérifiés avant chaque appel. Les désaccords entre agents deviennent des artefacts escaladés. Le canevas d'architecture devient éditable, et le modifier change réellement les outils disponibles à l'exécution.

La preuve est de passer la publication en validation obligatoire, de constater l'arrêt du cycle au bon endroit, puis de la repasser en automatique et de voir le cycle aller au bout.

## Surprises et découvertes

Rien à signaler pour l'instant.

## Journal des décisions

Le 16/09/2026, choix de PostgreSQL dès le départ plutôt que SQLite, décidé par l'utilisateur. Motif : la file de jobs s'appuie sur `FOR UPDATE SKIP LOCKED`, le temps réel sur `LISTEN/NOTIFY`, et la mémoire sémantique future sur pgvector.

Le 16/09/2026, choix de React et Vite plutôt qu'un rendu serveur, décidé par l'utilisateur. Motif : le canevas d'architecture interactif est une fonctionnalité de premier plan et non un ornement.

Le 16/09/2026, choix de ne pas faire du Manager un processus vivant. Motif : sans cela, les commandes de contrôle ne sont pas fiables et un redémarrage perd le travail en cours.

Le 16/09/2026, choix d'écarter le seuil de confiance auto-déclarée du modèle comme critère d'escalade. Motif : cette valeur n'est pas calibrée et le modèle converge vers la valeur qui laisse passer l'action. Les critères retenus sont le coût, l'irréversibilité, la nouveauté et le désaccord entre agents.

## Bilan

À compléter à la fin de l'exécution.
