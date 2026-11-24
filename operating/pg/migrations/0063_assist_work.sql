-- 0063_assist_work: Assist's work tables (ASSIST specs 1.1 deltas, 1.2 C5, 1.3 C8; all additive).
-- Readings: the actor and sender as staff ids (actor_person, actor_basis, sender_person), when
-- the capture first held the message (received_at), and assist.reading_by_handle, the
-- handle-keyed face every consumer reads (MODEL's reply alert included).
-- C5: assist.matters (one open piece of a person's work, re-verified every cycle) and its log,
-- assist.actions (what people did), assist.precedents (each act paired with what it answered),
-- assist.routines, assist.worker_runs (every model run with its cost), assist.scores.
-- C8: assist.write_classes (class A / B / C per system and operation; filled from
-- operating/write_classes.py) and assist.write_journal (the one journal; its CHECKs make
-- "only class A can ever be automatic" and "a performed write was confirmed by a named person
-- or was class A automatic" database facts).
-- Every reader role's grant is revoked (people reach Assist rows only through the API);
-- plx_eval may select (agents and the nightly score read everything, owner 11-23).
-- Undo: DROP the tables and views named here (assist.findings_feed too); ALTER TABLE assist.readings DROP the four columns.
ALTER TABLE assist.readings ADD COLUMN IF NOT EXISTS actor_person text;
ALTER TABLE assist.readings ADD COLUMN IF NOT EXISTS actor_basis text NOT NULL DEFAULT '';
ALTER TABLE assist.readings ADD COLUMN IF NOT EXISTS sender_person text;
ALTER TABLE assist.readings ADD COLUMN IF NOT EXISTS received_at timestamptz;
CREATE INDEX IF NOT EXISTS readings_actor ON assist.readings (actor_person, said_at) WHERE needs_act;
CREATE OR REPLACE VIEW assist.reading_by_handle AS SELECT h AS message_handle, r.* FROM assist.readings r, unnest(r.handles) AS h;
CREATE TABLE IF NOT EXISTS assist.matters (
  matter_id text PRIMARY KEY,                -- left(encode(sha256(owner_person||'|'||kind||'|'||subject_key),'hex'),24)
  owner_person text NOT NULL,                -- staff id; for a role pool the pool id 'pool:<role>'
  kind text NOT NULL CHECK (kind IN ('conversation','approval','task','record','routine','meeting','promise','wait')),
  subject_key text NOT NULL,                 -- thread_key | approval id | task id | alert id | routine_id||'|'||trigger key | meeting id
  sources jsonb NOT NULL,                    -- {"readings":[[source,message_key]..],"records":[handle..],"alert":id,"routine":id,"still_true":[{view,handle,column,op,value}..]}
  basis_at timestamptz NOT NULL,             -- when the thing that makes it theirs happened (the asking message, the request, the record change)
  opened_at timestamptz NOT NULL DEFAULT now(),
  title text NOT NULL, detail text NOT NULL DEFAULT '',
  next_act text CHECK (next_act IN ('reply','forward','new_message','attach','system_write','ask_person','nothing')),
  due_at timestamptz,
  counterpart text NOT NULL DEFAULT '', counterpart_party text,
  audiences jsonb NOT NULL DEFAULT '[]', markings jsonb NOT NULL DEFAULT '[]',   -- carried from every source row, as items carry them today
  state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','unverified','done','dropped')),
  done_reason text, done_at timestamptz,
  verified_at timestamptz, verify_note text NOT NULL DEFAULT '',
  plan jsonb,                                -- the worker's plan and prepared acts (2.6)
  plan_status text NOT NULL DEFAULT 'none' CHECK (plan_status IN ('none','planned','drafted','needs_you','deferred','failed')),
  plan_reason text NOT NULL DEFAULT '',      -- typed: see section 5
  plan_ver smallint, plan_at timestamptz, ready_at timestamptz,
  legacy_item_id text,
  UNIQUE (owner_person, kind, subject_key));
CREATE INDEX IF NOT EXISTS matters_owner_open ON assist.matters (owner_person, due_at) WHERE state IN ('open','unverified');
CREATE INDEX IF NOT EXISTS matters_subject ON assist.matters (kind, subject_key);

CREATE TABLE IF NOT EXISTS assist.matter_log (
  seq bigserial PRIMARY KEY, matter_id text NOT NULL, at timestamptz NOT NULL DEFAULT now(),
  what text NOT NULL CHECK (what IN ('opened','basis_moved','verified','unverified','closed','planned','drafted','plan_failed','deferred','shown','requested','person_line')),
  detail jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS matter_log_matter ON assist.matter_log (matter_id, seq);

CREATE TABLE IF NOT EXISTS assist.actions (
  action_key text PRIMARY KEY,               -- day_coverage.action_key
  person text NOT NULL, at timestamptz NOT NULL,
  system text NOT NULL,                      -- mail|teams|mission_control|planner|calendar|sharepoint|sage|filemaker|bc|portal
  kind text NOT NULL,                        -- day_coverage kinds: mail_sent, teams_sent, approval, question, planner_created, sharepoint_edit, sage_ar_invoice, ...
  target_type text NOT NULL DEFAULT '', target_handle text NOT NULL DEFAULT '',
  source text, message_key text, thread_key text,   -- for sends: the message, as in assist.readings
  recipients text[] NOT NULL DEFAULT '{}', has_attachment boolean,
  counterpart text NOT NULL DEFAULT '', counterpart_party text,
  fields jsonb NOT NULL DEFAULT '{}',        -- for system writes: field -> value as keyed
  attributed_by text NOT NULL,               -- 'sender' | 'created_by' | 'editor' | 'enteredby:unique' | 'tested_by:unique' ...
  not_human text NOT NULL DEFAULT '',        -- day_coverage.not_human reason, '' when a person's act
  loaded_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS actions_person_at ON assist.actions (person, at);
CREATE INDEX IF NOT EXISTS actions_kind_at ON assist.actions (kind, at);
CREATE INDEX IF NOT EXISTS actions_target ON assist.actions (target_handle) WHERE target_handle <> '';
CREATE INDEX IF NOT EXISTS actions_thread ON assist.actions (thread_key, at) WHERE thread_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS assist.precedents (
  action_key text PRIMARY KEY,               -- the assist.actions key of the act; 'none:<source>:<message_key>:<person>' for an asked-and-not-acted case (no actions row, so no foreign key)
  person text NOT NULL, role text NOT NULL DEFAULT '',
  at timestamptz NOT NULL,                   -- when the person acted (or basis_at + horizon for 'nothing')
  trigger_kind text NOT NULL CHECK (trigger_kind IN ('message','record','time','none')),
  trigger_source text, trigger_message_key text, trigger_at timestamptz,
  thread_key text, channel text NOT NULL CHECK (channel IN ('email','chat','mission_control','system')),
  counterpart text NOT NULL DEFAULT '', counterpart_party text,
  act text NOT NULL CHECK (act IN ('reply','forward','new_message','attach','system_write','nothing')),
  act_to text[] NOT NULL DEFAULT '{}',       -- who it went to (staff ids or outside addresses), or {system:operation}
  latency_s integer,
  situation text NOT NULL,                   -- what is embedded: see 2.5 step 5
  situation_sha text NOT NULL,               -- key into search.chunk_vectors kind 'precedent'
  act_text text NOT NULL DEFAULT '',         -- the person's own words of the act (own text, quoted thread removed); '' for system writes
  act_fields jsonb NOT NULL DEFAULT '{}',
  new_facts boolean,                         -- NULL until judged (2.6 step 6): did the act state values no record or thread held
  built_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS precedents_person_at ON assist.precedents (person, at);
CREATE INDEX IF NOT EXISTS precedents_person_party ON assist.precedents (person, counterpart_party, at);
CREATE INDEX IF NOT EXISTS precedents_role_at ON assist.precedents (role, at);

CREATE TABLE IF NOT EXISTS assist.routines (
  routine_id text PRIMARY KEY,               -- left(sha256(scope||'|'||kind||'|'||trigger),'hex',16)
  scope text NOT NULL,                       -- staff id, or 'role:<role>' when pooled
  kind text NOT NULL,                        -- the action kind it anticipates
  trigger jsonb NOT NULL,                    -- {"event":<view.change>|"time","relation":<link path>,"lag_s":{p50,p90},"horizon_s":n}
  field_sources jsonb NOT NULL DEFAULT '{}', -- field -> {"from":"trigger.<col>"|"record.<view>.<col>"|"constant"|"person", "equal_share":0.97,"n":41}
  description text NOT NULL DEFAULT '',      -- the model's plain write-up, shown to the person
  mined jsonb NOT NULL,                      -- {"since","until","acts":n,"days":n}
  validated jsonb NOT NULL DEFAULT '{}',     -- {"since","until","fired":n,"acted":n,"anticipated":n,"acts":n}
  status text NOT NULL CHECK (status IN ('candidate','passed','failed','live','retired')),
  status_reason text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS assist.worker_runs (
  seq bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
  matter_id text, person text NOT NULL, tier text NOT NULL CHECK (tier IN ('plan','draft','from_line','sample','judge','label','routine')),
  shadow boolean NOT NULL, host text NOT NULL, model text NOT NULL,
  usd numeric(10,4), usd_basis text NOT NULL CHECK (usd_basis IN ('reported','estimate')),
  seconds double precision, status text NOT NULL CHECK (status IN ('ok','failed','deferred','screened')),
  reason text NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS worker_runs_day ON assist.worker_runs (at, tier);

CREATE TABLE IF NOT EXISTS assist.scores (
  day date NOT NULL, person text NOT NULL, role text NOT NULL, kind text NOT NULL,
  measure text NOT NULL,                     -- actions | anticipated | right_act | usable | usable_after_line | on_time | anticipatable | shown | shown_real | open_sampled | open_true
  n integer NOT NULL, m integer NOT NULL, matching text NOT NULL, not_measured text NOT NULL DEFAULT '',
  PRIMARY KEY (day, person, kind, measure));
CREATE TABLE IF NOT EXISTS assist.write_classes (
  system text NOT NULL, operation text NOT NULL,
  class char(1) NOT NULL CHECK (class IN ('A','B','C')),
  b_rule text NOT NULL DEFAULT '' CHECK (b_rule IN ('','mc_not_routine','message_has_figures','external_attendee')),
  why text NOT NULL,
  PRIMARY KEY (system, operation));

CREATE TABLE IF NOT EXISTS assist.write_journal (
  seq bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
  prepared_id text NOT NULL, person text NOT NULL,
  origin text NOT NULL CHECK (origin IN ('assist','ask','card','upload','instruction')),
  matter_id text,
  system text NOT NULL, operation text NOT NULL, class char(1) NOT NULL CHECK (class IN ('A','B','C')),
  event text NOT NULL CHECK (event IN ('prepared','previewed','confirmed','performed','failed','unknown','dismissed','undone','refused','expired','sheet_shown','sheet_matched','sheet_mismatch')),
  target jsonb NOT NULL DEFAULT '{}', before jsonb, after jsonb,
  sources jsonb NOT NULL DEFAULT '{}',       -- field -> {"from": record handle | [source,message_key] | "person", "quote": ...}
  undo jsonb, refusal jsonb,                 -- refusal = {"problems":[..],"status":n} : the one refusal type (prepared_writes.Refused)
  preview_hash text, idempotency_key text,
  auto boolean NOT NULL DEFAULT false, confirmed_by text,
  CHECK (NOT (auto AND class <> 'A')),                                   -- only class A can ever be automatic
  CHECK (event <> 'performed' OR auto OR confirmed_by IS NOT NULL),      -- a performed write was confirmed by a named person or was class A auto
  CHECK (class <> 'C' OR event IN ('prepared','sheet_shown','sheet_matched','sheet_mismatch','dismissed','expired','refused')));  -- class C is never performed by us
CREATE UNIQUE INDEX IF NOT EXISTS write_journal_once ON assist.write_journal (idempotency_key) WHERE event = 'performed';
CREATE INDEX IF NOT EXISTS write_journal_prepared ON assist.write_journal (prepared_id, seq);
CREATE INDEX IF NOT EXISTS write_journal_person ON assist.write_journal (person, at);
REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM PUBLIC;
DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_sql_reader') THEN
        REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM plx_sql_reader;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT USAGE ON SCHEMA assist TO plx_eval;
        GRANT SELECT ON ALL TABLES IN SCHEMA assist TO plx_eval;
    END IF;
END
$migration$;
-- The findings feed: the one face of the readings the API role may read (operating/pg/
-- comms_findings.py FEED): USAGE on the schema and SELECT on this view only, after the revokes.
CREATE OR REPLACE VIEW assist.findings_feed AS SELECT source, handles, said_at, from_side, findings FROM assist.readings WHERE findings <> '[]'::jsonb;
REVOKE ALL ON assist.findings_feed FROM PUBLIC;
DO $feed$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN GRANT USAGE ON SCHEMA assist TO plx_api; GRANT SELECT ON assist.findings_feed TO plx_api; END IF; END $feed$;
