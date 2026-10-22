# Open decision (ovs-teams): Teams lineage and whose chats are captured

1. **Lineage.** `integrations/capture_teams.py` stores Teams channel and chat messages in the
   capture store, whose records must carry a C0 lineage (`ontology/store_ddl.py` `LINEAGES`,
   frozen format, owned by the ontology package). There is no `teams` lineage. Options: (a) `mail`
   (person-to-person messages; the kind `teams_channel`/`teams_chat` and the
   `teams-channel://`/`teams-chat://` handle keep the exact source); (b) add `teams` to
   `LINEAGES` and `_format.schema.json` (a C0 format change: ontology package, schema drift check);
   (c) `original`. Evidence so far: lineage only selects authority/precedence rules; no
   ontology binding reads Teams yet. Default until decided: (a), switched to (b) when the
   ontology package adds the lineage (a re-capture is not needed: the projector maps by kind).
2. **Whose chats.** The capture can list a member's chats (`members[].chats`). The repo scope
   (`integrations/teams_capture_scope.json`) captures only the channels of the teams cos@ joined,
   chats off; the live check reads the channels of cos@'s and vince@'s teams into its temporary
   state, chats off too until this is decided. Options: (a) channels only by default, chats per named account on an
   owner decision (as mail's allowlist was); (b) every allowlisted account's chats. Evidence:
   the mail allowlist (`operating/case_mail_scope.json`) was an owner decision per mailbox; no
   such decision exists for Teams. Default: (a).
3. **Teams id grammars.** Identifier grammars live in `ontology/schema/identifiers.json`
   (`system_kinds`; `ontology.tests.test_identifiers` forbids private ones in
   `live_source_grammar.py`), owned by the ontology package. Until it adds `teams_thread_id`
   (`19:<id>@thread.tacv2|thread.v2|thread.skype|unq.gbl.spaces`) and `teams_message_id`
   (digits), the grammar validates a thread id as `19:` + a `graph_mail_id` + one enumerated
   suffix and a Teams message id as a `graph_mail_id` (same safe character set: no `/`, `.`,
   `%`, `\`). Default: switch to the dedicated kinds when they exist.

- raised: 2026-10
- decided: 1. lineage (a) `mail`, already built (`integrations/capture_teams.py:47-48`; `LINEAGES` in `ontology/store_ddl.py:47-49`). 2. whose chats: (b) every account on the owner-approved mail allowlist (`operating/case_mail_scope.json`), by owner policy. 3. grammars: dedicated `teams_thread_id`/`teams_message_id` `system_kinds` in `ontology/schema/identifiers.json`.
- evidence: `integrations/live_source_grammar.py:183-184` holds the teams_thread and teams_message_id params; the message id reuses `_MAIL_ID` at :184 (defined :57; the task cited :183, the reuse is on :184).
- follow-up (open): the dedicated grammars wait until `ontology/schema/identifiers.json` is free.
- status: decided
