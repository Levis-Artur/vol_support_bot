-- Run after schema.sql, in the same transaction, with the old bot stopped.
-- No user is unblocked and no ticket payload, ID, timestamp of creation or photo is removed.
ALTER TABLE tickets ALTER COLUMN status SET DEFAULT 'new';

-- Keep legacy verified data, but remove its relationship with blocked.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_input_limits;
ALTER TABLE users ADD CONSTRAINT users_input_limits CHECK (
 telegram_id > 0 AND char_length(full_name) <= 100 AND char_length(department) <= 100
) NOT VALID;
ALTER TABLE user_events DROP CONSTRAINT IF EXISTS user_events_action_check;
ALTER TABLE user_events ADD CONSTRAINT user_events_action_check
 CHECK (action IN ('approve','reject','block','unblock'));

-- Also used when an administrator explicitly unblocks an author of legacy tickets.
-- UPDATE predicate and RETURNING keep event insertion idempotent, including retries.
CREATE OR REPLACE FUNCTION release_legacy_tickets(
 target_user BIGINT DEFAULT NULL, actor BIGINT DEFAULT NULL
) RETURNS BIGINT LANGUAGE plpgsql AS $$
DECLARE released BIGINT;
BEGIN
 WITH candidates AS (
  SELECT t.id, row_number() OVER (ORDER BY t.id) AS position
  FROM tickets t JOIN users u ON u.telegram_id=t.creator_id
  WHERE t.status='pending_verification' AND NOT u.blocked
    AND (target_user IS NULL OR t.creator_id=target_user)
 ), promoted AS (
  UPDATE tickets t SET status='new', updated_at=now(),
    next_delivery_at=now() + candidates.position * interval '10 seconds'
  FROM candidates
  WHERE t.id=candidates.id AND t.status='pending_verification'
  RETURNING t.id
 )
 INSERT INTO ticket_events(ticket_id,actor_id,old_status,new_status)
 SELECT id,actor,'pending_verification','new' FROM promoted;
 GET DIAGNOSTICS released = ROW_COUNT;
 RETURN released;
END;
$$;

-- Queues work only. The application worker performs delivery later in bounded batches.
SELECT release_legacy_tickets();
