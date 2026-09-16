-- Agent Studio — schéma MVP (PostgreSQL / Neon)
create extension if not exists pgcrypto;

create table if not exists agents (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  description  text not null default '',
  role         text not null default 'specialist',
  instructions text not null default '',
  model        text not null default 'gpt-4.1-mini',
  temperature  real,
  accent       text not null default 'slate',
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create table if not exists agent_memory (
  id         uuid primary key default gen_random_uuid(),
  agent_id   uuid not null references agents(id) on delete cascade,
  content    text not null,
  source     text not null default 'human',
  created_at timestamptz not null default now()
);

create table if not exists projects (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  objective     text not null default '',
  state         text not null default 'IDLE',
  max_revisions int  not null default 2,
  autonomy      jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table if not exists project_agents (
  project_id uuid not null references projects(id) on delete cascade,
  agent_id   uuid not null references agents(id) on delete cascade,
  role       text not null default 'specialist',
  x          real not null default 0,
  y          real not null default 0,
  primary key (project_id, agent_id)
);

create table if not exists architecture_edges (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete cascade,
  source_agent_id uuid not null references agents(id) on delete cascade,
  target_agent_id uuid not null references agents(id) on delete cascade,
  kind            text not null default 'calls',
  unique (project_id, source_agent_id, target_agent_id)
);

create table if not exists cycles (
  id         uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  brief      text not null default '',
  status     text not null default 'queued',
  step_count int  not null default 0,
  demo       boolean not null default false,
  created_at timestamptz not null default now(),
  ended_at   timestamptz
);

create table if not exists messages (
  id         bigserial primary key,
  project_id uuid not null references projects(id) on delete cascade,
  cycle_id   uuid references cycles(id) on delete cascade,
  agent_id   uuid references agents(id) on delete set null,
  agent_name text not null default '',
  kind       text not null default 'note',
  depth      int  not null default 0,
  title      text not null default '',
  body       text not null default '',
  payload    jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists artifacts (
  id         uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  cycle_id   uuid references cycles(id) on delete cascade,
  agent_id   uuid references agents(id) on delete set null,
  agent_name text not null default '',
  type       text not null default 'content',
  title      text not null default '',
  body       text not null default '',
  data       jsonb not null default '{}'::jsonb,
  status     text not null default 'draft',
  created_at timestamptz not null default now()
);

create table if not exists decisions (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  cycle_id    uuid references cycles(id) on delete cascade,
  kind        text not null default 'approval',
  title       text not null default '',
  detail      text not null default '',
  payload     jsonb not null default '{}'::jsonb,
  status      text not null default 'pending',
  response    text not null default '',
  created_at  timestamptz not null default now(),
  resolved_at timestamptz
);

create table if not exists jobs (
  id         bigserial primary key,
  project_id uuid not null references projects(id) on delete cascade,
  cycle_id   uuid references cycles(id) on delete cascade,
  kind       text not null default 'run_step',
  status     text not null default 'queued',
  attempts   int  not null default 0,
  run_after  timestamptz not null default now(),
  locked_at  timestamptz,
  payload    jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists run_states (
  cycle_id   uuid primary key references cycles(id) on delete cascade,
  state_json jsonb not null,
  agent_name text not null default '',
  updated_at timestamptz not null default now()
);

create table if not exists facts (
  id         uuid primary key default gen_random_uuid(),
  project_id uuid not null references projects(id) on delete cascade,
  label      text not null,
  value      text not null,
  created_at timestamptz not null default now()
);

create index if not exists messages_stream_idx  on messages (project_id, id);
create index if not exists messages_cycle_idx   on messages (cycle_id, id);
create index if not exists jobs_queue_idx       on jobs (status, run_after);
create index if not exists artifacts_proj_idx   on artifacts (project_id, created_at desc);
create index if not exists decisions_proj_idx   on decisions (project_id, status);
