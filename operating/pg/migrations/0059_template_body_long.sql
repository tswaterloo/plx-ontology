-- 0059_template_body_long: a saved template's body holds up to 100,000 characters.
--
-- 0022 bound templates.template.body to 8,000 characters, so a template proposed from a long
-- draft (the app proposes from up to 100,000) could not be saved whole. This replaces the
-- body's CHECK with the same rule at 100,000 (operator_api.templates.MAX_BODY). Widening
-- only: every row 0022 allowed is still allowed; no row is rewritten. 0022 stays as applied.
-- A database without templates.template is left alone.
-- Undo: drop template_body_check and add it back with length(body) <= 8000 (only once no
-- body is longer).
DO $migration$
DECLARE
    old_check text;
BEGIN
    IF to_regclass('templates.template') IS NULL THEN
        RETURN;
    END IF;
    FOR old_check IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'templates.template'::regclass AND contype = 'c'
           AND pg_get_constraintdef(oid) LIKE '%length(body)%'
    LOOP
        EXECUTE format('ALTER TABLE templates.template DROP CONSTRAINT %I', old_check);
    END LOOP;
    ALTER TABLE templates.template ADD CONSTRAINT template_body_check
        CHECK (body <> '' AND length(body) <= 100000);
END
$migration$;
