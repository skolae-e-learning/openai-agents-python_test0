-- Lot 3 — connexions externes et images de cycle.
--
-- Additive et rejouable. `schema.sql` décrit une installation neuve ; ce fichier
-- fait passer une base déjà en service. Les deux doivent rester d'accord.

-- Rattrapage : ces colonnes de `cycles` vivaient en production sans figurer dans
-- schema.sql. Sans effet là où elles existent déjà.
alter table cycles add column if not exists plan      jsonb not null default '[]'::jsonb;
alter table cycles add column if not exists cursor    int  not null default 0;
alter table cycles add column if not exists revisions int  not null default 0;
alter table cycles add column if not exists note      text not null default '';

-- Une connexion appartient à une personne et n'est jamais partagée : pas de
-- colonne `visibility`, et donc pas de lecture par `readable()`.
create table if not exists connectors (
  id             uuid primary key default gen_random_uuid(),
  owner_id       text not null,
  kind           text not null,                       -- clé de CONNECTOR_KINDS
  name           text not null,
  capabilities   text[] not null default '{}',
  config         jsonb  not null default '{}'::jsonb, -- non secret : url, adresses, dépôt…
  secret_enc     text   not null default '',          -- jeton Fernet, jamais renvoyé par l'API
  status         text   not null default 'untested',  -- untested | ok | error
  status_detail  text   not null default '',
  last_tested_at timestamptz,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists connectors_owner_idx on connectors (owner_id);

-- `token` sert d'URL capacitaire : une balise <img> ne peut pas porter d'en-tête
-- Authorization, donc l'accès aux octets tient à un identifiant non devinable.
create table if not exists cycle_images (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  cycle_id     uuid references cycles(id) on delete cascade,
  token        uuid not null default gen_random_uuid(),
  filename     text not null default '',
  content_type text not null default 'image/png',
  bytes        bytea not null,
  caption      text not null default '',
  created_at   timestamptz not null default now()
);
create index if not exists cycle_images_cycle_idx on cycle_images (cycle_id, created_at);
