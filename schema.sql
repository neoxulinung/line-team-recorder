CREATE TABLE topics (
  id TEXT PRIMARY KEY,
  line_group_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,              -- 'active' | 'ended'
  owner_line_user_id TEXT NOT NULL,
  organize_prompt TEXT,              -- NULL = use organize.py's GENERIC_ORGANIZE_PROMPT; per-topic
                                      -- customization is Phase 3 (LIFF editor), column exists now
  -- Comma-separated module names enabled at /開始 time (e.g. "記帳,投票"), not one column per
  -- module - see modules.py. Adding a future module never needs a schema migration this way.
  enabled_modules TEXT NOT NULL DEFAULT '',
  started_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE UNIQUE INDEX idx_topics_one_active_per_group ON topics (line_group_id) WHERE status = 'active';

CREATE TABLE topic_messages (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  line_message_id TEXT NOT NULL UNIQUE,   -- de-dupes webhook redelivery
  line_user_id TEXT NOT NULL,
  user_display_name TEXT,
  text TEXT,
  sent_at INTEGER NOT NULL,
  organized_at INTEGER               -- NULL = not yet folded into topic_docs
);
CREATE INDEX idx_topic_messages_pending ON topic_messages (topic_id, organized_at);

CREATE TABLE topic_docs (
  topic_id TEXT PRIMARY KEY REFERENCES topics(id),
  content_md TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE topic_doc_revisions (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  content_md TEXT NOT NULL,
  triggered_by_message_id TEXT REFERENCES topic_messages(id),  -- set for an AI-organize revision
  edited_by_user_id TEXT,            -- set for a manual edit / restore (Phase 3, unused until then)
  edited_by_display_name TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE doc_fact_check_flags (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  claim TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_fact_check_flags_topic ON doc_fact_check_flags (topic_id, created_at);

-- Phase 2: 記帳 module. Direct copy of itineraryManager's expenses/expense_splits structure,
-- trip_id -> topic_id.
CREATE TABLE topic_expenses (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  payer_line_user_id TEXT NOT NULL,
  payer_display_name TEXT,
  amount REAL NOT NULL,
  description TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX idx_topic_expenses_topic ON topic_expenses (topic_id);

CREATE TABLE topic_expense_splits (
  expense_id TEXT NOT NULL REFERENCES topic_expenses(id),
  line_user_id TEXT NOT NULL,
  display_name TEXT,
  PRIMARY KEY (expense_id, line_user_id)
);

-- Phase 2: 投票 module. Direct copy of itineraryManager's polls/poll_options/poll_votes
-- structure, trip_id -> topic_id - except polls.topic is renamed `question` here, since
-- "topic" already means something else (this app's own core concept) in this project.
CREATE TABLE topic_polls (
  id TEXT PRIMARY KEY,
  topic_id TEXT NOT NULL REFERENCES topics(id),
  question TEXT NOT NULL,
  status TEXT NOT NULL, -- 'active' | 'ended'
  created_by_line_user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE INDEX idx_topic_polls_topic ON topic_polls (topic_id, created_at);
CREATE UNIQUE INDEX idx_topic_polls_one_active_per_topic ON topic_polls (topic_id) WHERE status = 'active';

CREATE TABLE topic_poll_options (
  id TEXT PRIMARY KEY,
  poll_id TEXT NOT NULL REFERENCES topic_polls(id),
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_topic_poll_options_poll ON topic_poll_options (poll_id, created_at);

CREATE TABLE topic_poll_votes (
  poll_option_id TEXT NOT NULL REFERENCES topic_poll_options(id),
  line_user_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  voted_at INTEGER NOT NULL,
  PRIMARY KEY (poll_option_id, line_user_id)
);

CREATE TABLE llm_usage (
  id TEXT PRIMARY KEY,
  topic_id TEXT,
  purpose TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  estimated_cost_usd REAL NOT NULL,
  created_at INTEGER NOT NULL
);
