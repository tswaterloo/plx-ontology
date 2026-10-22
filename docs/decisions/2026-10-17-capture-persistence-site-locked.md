# 2026-10-17: a locked personal OneDrive is a counted, typed state, not a discovery gap

Base `f635203`. It replaces the open record of 2026-10-16T17:50Z.

**Question.** `capture.sharepoint_estate` requires `estate.discovery_complete`. The live run
of 2026-10-16 failed it on one discovery gap, `library_listing:http_423`: the drives listing
of one personal site (a OneDrive on `<tenant>-my.sharepoint.com`) answered HTTP 423 Locked.
Is a locked personal site a gap (fails the check) or a typed, counted state?

**Options.**
1. Keep it a gap. The check fails every day until someone unlocks the OneDrive or adds the
   site to the exclusion list by hand.
2. Personal-only typed state (the shipped default): a personal site whose library listing
   answers 423 (`http_423` or `retried.http_423`) is counted `sites_locked`, its
   `sites_seen` state is `locked`, and a registry site in that state is reported `failed`
   with reason `site_locked`. A 423 from a site on the configured SharePoint site host stays
   the `library_listing:http_423` gap.
3. Treat 423 as a gap on every host. For the evidence held this is option 1.

**Decision.** Option 2. It lives in `integrations/capture_sharepoint.py` (`LOCKED_DETAILS`
and the `personal` guard in the library-listing step). `integrations/tests/test_capture_sharepoint_estate.py`
(`test_a_locked_personal_site_is_counted_never_a_gap`) covers both hosts: the personal site
is counted, the site-host one stays a named gap.

**Reasons.**
- Microsoft archives unlicensed OneDrives beyond any app's reach until an admin reactivates
  them; nothing can be read from a locked one, so nothing readable is skipped.
- A personal site has no `/sites` or `/teams` name, so option 1's hand exclusion cannot
  single it out and the check would fail daily.
- Every run re-tests the lock, so an unlocked OneDrive is walked again.
- `estate.sites_locked` is a required json_field of
  `verify/live-checks/capture.sharepoint_estate.json`, so the count stays visible.
- Option 3 is option 1 for the evidence held.

**Known weakness.** `sites_locked` has no upper bound against `sites_personal`, so a mass
423 would pass the check. Follow-up: `estate-locked-ceiling`.

**Reverse the decision if**
- a licensed, active user's OneDrive answers 423;
- `sites_locked` becomes a large share of `sites_personal`; or
- the owner puts archived OneDrive content in scope.

**Evidence label.** Live-unverified: the 423 cause (retention or archive) is inferred from
Microsoft documentation, not confirmed for this tenant; the site's name is not recorded.
