# Decision 2026-10-07: MC-shared projects keep the explicit project grant

- decided: 2026-10-07
- status: decided

## Question

Every MC task, approval gate, task event and bucket needs
`mc:<scope>:project:<project>` on top of `mc:<scope>:<collection>`, also for projects MC
treats as shared (`visibility != "restricted"`) and for `unparented` buckets. A live audience
that grants only the collection loses all of these records without a word. Should shared
projects be readable with the collection grant alone?

## Decision

No. Keep the explicit project grant and fail closed. Make the loss visible instead:

- The capture counts the records that no principal granted the collection may read for want
  of their project grant (`withheld_project_grant` per unit summary in
  `integrations/mc_sources.py`). The ingestion worker reports it as
  `withheld:project_grant:<n>` and `counts.withheld`, so the unit is not complete. If the
  audience cannot list its principals, it reports `withheld:project_grant:not_measured`,
  never zero.
- `mc.sources_capture` reports `_summary.project_trim: not_measured` when fewer than two
  projects were captured. Its `complete_when` records that result as incomplete, so a
  one-project MC never counts as a per-project pass.

## Evidence

- MC's rule is in PLX_MC@eeb7db7 `src/lib/permissions/project-acl.ts`. `projectVisibility`
  treats anything other than `restricted` as shared. `canAccessProject` admits every user
  to a shared project. `canAccessBucket` admits every user to an unparented bucket and
  refuses a dangling project id.
- No tool the capture may call returns project visibility. `actionGetContext` (depth full)
  returns `tasks` and ACL-filtered `buckets` carrying only a project id; it does not
  return `snap.projects`. `actionListBuckets` returns `project` as an id. No MC read tool
  in `integrations/mc_tool_policy.json` lists projects. So the Workbench cannot tell a
  shared project from a restricted one. Trusting the collection grant would expose every
  restricted project's records to anyone who holds the collection grant.
- The MCP identity is the operator. The capture holds the operator's view, so trimming per
  reader is a Workbench grant decision, not something MC already did for that reader.

## Options considered

1. Shared projects follow the collection grant. This needs project visibility captured
   from a read tool MC does not expose today (or a new MC read, an upstream change). Not
   possible now.
2. Keep the explicit grant and report the loss. This is the chosen option.
3. Grant every captured project wherever the collection is granted. This is the same leak
   as option 1.

## Consequences

- A live audience owner grants each project a reader may see, for example
  `"mc:<scope>:project:PRJ-OPS"` or `"mc:<scope>:project:unparented"`. A pattern such as
  `"mc:<scope>:*"` grants every project of the scope, restricted ones included. Updating
  the live audience files is a verifier or owner step: the credentialed environment holds
  those files, not this repo.
- Revisit this if MC exposes project visibility through a read tool. The capture can then
  record it per bucket, and shared projects can follow the collection grant.

## Related

- A task that the listing shows in another project has its current gates and events saved
  again under the new project in the same cycle (`McSourcesCapture._rekey_moved`). They no
  longer wait for the task-detail cursor. Two cases are unchanged:
  - Records already marked missing keep their old project key. Saving them again would
    clear the missing mark.
  - A task that leaves the operator's view entirely has no new project. Its gates and
    events keep the old key. The next complete detail traversal marks them missing.
- Minor 4 (capture store and projection use different old-version rules; both fail
  closed) is deferred.
