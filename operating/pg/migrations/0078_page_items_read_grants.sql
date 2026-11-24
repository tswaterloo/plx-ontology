-- 0078_page_items_read_grants: the batch runners (plan_batch, history_resident, the plan pull) read
-- biz.page_items itself as plx_eval, the read-only full reader; 0064 revoked that grant and
-- page_items.ensure replays 0064, so a rebuilt or re-ensured store had owner-only grants and every
-- runner looped on InsufficientPrivilege (KNOWN-GAPS O3). page_items.ensure now grants after
-- replaying 0064; this gives an existing database the same.
-- plx_api gets NOTHING: the table holds rows of every access class, and the API reads it only
-- through the guarded view biz_page.page_items and the two security-definer functions of 0064
-- (checked: no API code path names biz.page_items). Additive: a grant only; no view, value or
-- row changes.
-- Undo: REVOKE SELECT ON biz.page_items FROM plx_eval.
DO $migration$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
    GRANT SELECT ON biz.page_items TO plx_eval;
  END IF;
END
$migration$;
