# Agent Studio

Application de production de contenu par équipes d'agents IA. On crée des agents, on les réunit en projets, on dessine l'architecture qui dit lequel parle à lequel, puis on les laisse travailler avec la possibilité d'intervenir à tout moment.

L'architecture visée est décrite dans `ARCHITECTURE.md`, le plan d'exécution dans `EXECPLAN.md`.

## Ce qui tourne

Le front est une application React servie en statique. L'API est une fonction serverless Python (FastAPI) dans `api/index.py`. L'état durable est dans PostgreSQL, dont le schéma est dans `db/schema.sql`.

Un appel à `POST /api/tick` exécute exactement un step d'orchestration puis rend la main. C'est ce qui permet de faire tourner une équipe d'agents sur une plateforme sans processus long : le chef d'orchestre n'est jamais un processus vivant, il est reconstruit à chaque step depuis la base.

## Configuration

`DATABASE_URL` est obligatoire et pointe vers la base PostgreSQL. `OPENAI_API_KEY` est facultative : sans elle l'application tourne en mode démo, où le parcours, les écritures, la boucle de révision et la suspension pour validation sont réels mais le texte produit par les agents est fabriqué localement. Avec elle, les agents sont exécutés par le SDK `openai-agents`.

## Développement local

Installer les dépendances avec `npm install`, lancer l'API avec `uvicorn api.index:app --port 8000`, puis `npm run dev`. Le serveur de développement relaie `/api` vers le port 8000.
