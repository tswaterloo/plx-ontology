# Decision 2026-10-08: A committed per-bucket audience file gives the MC follow-up audience

- decided: 2026-10-08 within owner policy
- status: decided

## Question

`ActionPath` quotes a source in an MC follow-up task only when every member of the target bucket's audience reads it (`mc_follow_up_audience`, `operating/goal2_mc_approval.py`, `operating/action_path.py` `_audience_reads`). Which provider gives a production host that audience?

## Decision

Option 2. A committed file `operating/mc_bucket_audiences.json`, loaded like `operating/approver_roster.json`, names for each MC follow-up bucket every member who reads it: email, Entra oid, where read, read_at (amended 2026-10-13 below: visibility and every MC member token). The host passes its loader as `mc_follow_up_audience`. A null oid, a bucket not in the file, or an entry past its re-read age gives no audience. The follow-up then carries citations only (`text withheld: audience rights unresolved`). The send-time recheck (`operating/action_path.py` ~:2376-2389) stays.

## Evidence

- MC has no read of project members. PLX_MC@eeb7db7: `src/app/api/projects/route.ts:23` is POST only, `[id]/route.ts:29` is PATCH only, and no MCP tool lists projects.
- Shared and unparented buckets are open to every signed-in user (`src/lib/permissions/project-acl.ts:97-108`). Sign-in is `PLX_MC_ALLOWED_USERS` (`src/lib/auth/gate.ts:10`). The owner therefore states those audiences in the file.

## Amendment 2026-10-13: principal kinds

The first file keyed members by human email only and treated a shared bucket's audience as every `PLX_MC_ALLOWED_USERS` user. MC reads by principal tokens, not by people. PLX_MC@e1ed268:

- A restricted project admits a principal that holds one of its member tokens: an email, an Entra oid, a directory id or a reviewed MCP service principal (`src/lib/permissions/project-acl.ts:1-5`, `:31-34`, `:75-83`).
- A shared project, or an unparented bucket, is open to every principal (`project-acl.ts:97-108`). That includes the agent principals `sp_mcp_*` (`src/lib/permissions/types.ts:143-155`).
- A directory id is a key of MC's directory (`src/lib/mc-data/data.ts:88`). Every MCP agent whose operator email maps to it holds that id too (`src/lib/routing/mutations/actors.ts:132-147`).

Rule, schema `plx-mc-bucket-audience/v2`: each bucket names its `visibility` (`restricted`, `shared` or `unparented`) and its members keyed by MC member token. Only a `restricted` bucket whose every token is a person (a human email, or an Entra oid with the same oid) gave an audience (narrowed to oid tokens on 2026-10-14), and only when every member has a sourced oid read within 30 days. A shared or unparented bucket, or one listing an `sp_*`, other service or directory-id token, loads and gives no audience: citations only. The loader refuses a key twice in one object, a token of no known kind, the same token twice in a bucket, and an oid on a non-person token. The 30-day `read_at` age stays (confirmed). A `read_at` up to 5 minutes ahead of the host clock counts as now (clock skew); further ahead is refused at load.

Known gap, upstream: an MCP agent's principal also carries the operator email from the `X-MC-Operator-Email` header, checked only against the allowlist (`src/lib/mcp/auth.ts:171-196`, `actors.ts:140-147`). So an agent key holder who names a listed person's email reads that restricted bucket. Superseded by the amendment of 2026-10-14 below.

## Amendment 2026-10-14: only oid tokens quote (gap-ea0d58992059)

- decided: the open decision on email tokens, closed.

Rule: only a `restricted` bucket whose every member token is an Entra oid gives a quoting audience. A bucket listing an email token loads, but its follow-ups carry citations only. Reason, PLX_MC@e1ed268: MC builds an MCP agent's ACL principal from the `X-MC-Operator-Email` header, which is checked only against the allowlist (`src/lib/mcp/auth.ts:171-181`, `src/lib/routing/mutations/actors.ts:140-147`). Any agent key holder who names a member's email would read the quoted text. No agent principal holds an oid.

Also:

- Tokens follow MC's `normalizeMemberToken` (`project-acl.ts:36-38`): trimmed and lower case. A plus-tagged email is its own token.
- An email token and an oid token that name one oid are one audience member, read at the older of the two reads. The only duplicate refused is the same token twice.
- A visibility that is not a string (a list, say) is the loader's typed `ValueError`: every host exits 2 with no traceback.

## Options not taken

- Option 1, a live read-only MC provider: it needs an upstream MC change to expose project members.
- Option 3, no provider: it drops the quoted source text that owner policy asks for.

## What would reverse it

MC exposes a read route for project members. Then adopt option 1 through the GET-only client, with a live-check spec, and retire the file.
