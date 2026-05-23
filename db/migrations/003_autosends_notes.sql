-- 003_autosends_notes.sql — additive JSONB column for proactive follow-up audit
-- See docs/cowork-targeted-integration-plan.md follow-up engine section.
--
-- Stores {silence_hours, label, draft_text, silence_window} for kind values
-- 'proactive_followup_drafted' / '_sent' / '_skipped'. Existing rows get
-- notes=NULL (harmless).

BEGIN;

ALTER TABLE autonomous_sends
  ADD COLUMN IF NOT EXISTS notes JSONB;

COMMIT;
