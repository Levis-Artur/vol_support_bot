CREATE TABLE IF NOT EXISTS users (
 telegram_id BIGINT PRIMARY KEY,
 full_name TEXT NOT NULL DEFAULT '',
 department TEXT NOT NULL DEFAULT '',
 verified BOOLEAN NOT NULL DEFAULT FALSE,
 blocked BOOLEAN NOT NULL DEFAULT FALSE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tickets (
 id BIGSERIAL PRIMARY KEY,
 creator_id BIGINT NOT NULL REFERENCES users(telegram_id),
 location TEXT NOT NULL,
 category TEXT NOT NULL,
 description TEXT NOT NULL,
 photo_file_id TEXT,
 status TEXT NOT NULL DEFAULT 'new'
  CHECK (status IN ('pending_verification','new','in_progress','waiting','done','cancelled')),
 assignee_id BIGINT REFERENCES users(telegram_id),
 group_message_id BIGINT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status,created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_creator ON tickets(creator_id,created_at DESC);
CREATE TABLE IF NOT EXISTS ticket_events (
 id BIGSERIAL PRIMARY KEY,
 ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
 actor_id BIGINT,
 old_status TEXT,
 new_status TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Additive, repeatable migration for existing installations; no data is deleted.
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS source_message_id BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS submitter_name TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS submitter_department TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS group_photo_message_id BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS group_synced_status TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS user_notified_status TEXT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS next_delivery_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_submission ON tickets(creator_id,source_message_id);
CREATE INDEX IF NOT EXISTS idx_ticket_delivery ON tickets(next_delivery_at,id);
CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events(ticket_id,id);

CREATE TABLE IF NOT EXISTS user_events (
 id BIGSERIAL PRIMARY KEY,
 user_id BIGINT NOT NULL REFERENCES users(telegram_id),
 actor_id BIGINT NOT NULL,
 action TEXT NOT NULL CHECK (action IN ('approve','reject','block','unblock')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NOT VALID preserves legacy rows while enforcing limits on every new/updated row.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='users'::regclass AND conname='users_input_limits') THEN
  ALTER TABLE users ADD CONSTRAINT users_input_limits CHECK (
   telegram_id > 0 AND char_length(full_name) <= 100 AND char_length(department) <= 100
  ) NOT VALID;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='tickets'::regclass AND conname='tickets_input_limits') THEN
  ALTER TABLE tickets ADD CONSTRAINT tickets_input_limits CHECK (
   char_length(location) BETWEEN 1 AND 120 AND char_length(description) BETWEEN 10 AND 1000
   AND category IN ('Комп''ютер','Принтер','Мережа','ПЗ','Інше')
   AND (submitter_name IS NULL OR char_length(submitter_name) BETWEEN 3 AND 100)
   AND (submitter_department IS NULL OR char_length(submitter_department) BETWEEN 2 AND 100)
   AND (photo_file_id IS NULL OR char_length(photo_file_id) <= 1024)
  ) NOT VALID;
 END IF;
END $$;
