-- ============================================================================
-- TikProfitPro: core logic pass (2026-11-14). Run in Supabase -> SQL Editor. Idempotent.
-- NOT applied by Hark. Nothing here changes or deletes existing rows.
--
-- 1) import_jobs: the admin upload's job status used to live only in the API process's memory,
--    so a Render restart / deploy mid-import lost it (the page then polled into a 404).
--    main.py saves every real import here; without this table it keeps working from memory only.
-- ============================================================================

create table if not exists public.import_jobs (
  id           text primary key,
  status       text not null check (status in ('running', 'done', 'failed')),
  started_by   text,
  started_at   timestamptz,
  finished_at  timestamptz,
  files        jsonb not null default '[]'::jsonb,
  summary      jsonb,
  warnings     jsonb not null default '[]'::jsonb,
  error        text,
  log          jsonb not null default '[]'::jsonb,
  updated_at   timestamptz not null default now()
);
create index if not exists import_jobs_started_idx on public.import_jobs (started_at desc);

-- Only the API (service_role, which bypasses RLS) reads or writes it. No anon / authenticated access.
alter table public.import_jobs enable row level security;
revoke all on public.import_jobs from anon, authenticated;

notify pgrst, 'reload schema';
