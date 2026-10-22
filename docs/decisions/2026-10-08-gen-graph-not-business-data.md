# gen-graph-not-business-data: which Microsoft Graph resources are not business data?

- raised: 2026-10-07 (gaps gap-c490b1863003, gap-4f8d661e9144 ING-11)
- context: the source registry lists every Graph request template the PLX repositories send
  (`microsoft-graph.endpoint:*`). `ontology/generator/graph.py` resolves each one against the
  pinned Graph v1.0 CSDL subset and classifies it by `ontology/generator/graph_families.json`.
  Resources a Workbench capture holds are bound (mail messages and attachments, drive items and
  a file's own list item, Teams channel and chat messages, SharePoint list items). The question
  was which of the others are not business data, so that their entries are listed with reason
  `not_business_data` and this record instead of `no_source_family`.
- options: (a) accept the defaults below; (b) treat `calendar_meetings` as business data
  (a calendar/transcript capture, its permissions Calendars.Read and
  OnlineMeetingTranscript.Read.All, a family and a live check); (c) treat `directory` as an
  ontology source (people and groups as objects) beside its use as permission input.
- evidence: the calendar and transcript templates come from agentic-swarm agent tooling
  (`apps/vmc-web/src/lib/vmc/calendar.ts`, `src/tools/meeting_ops.py`); no case
  names a meeting, a calendar entry or a directory object as evidence.
  Endpoint map: `ontology/schema/generated/meta/graph_endpoints.json`.
- decided (option a): the defaults hold.

  | Reason | Resources | Why |
  |---|---|---|
  | `container` | site, drive, list, columnDefinition, mailFolder, team, channel, chat, teamsTab, sharedDriveItem | The captures traverse them to reach items; they hold no business record of their own. |
  | `directory` | user, group, directoryObject, organization | Identity and membership: permission inputs (`integrations/graph_directory.py`), never ontology records. |
  | `infrastructure` | subscription | Change-notification subscriptions of the upstream sync engines. |
  | `presentation` | thumbnailSet, workbookRangeFormat, workbookRangeFont; list views (`/sites/{}/lists/{}/views`) | Rendering and formatting, not content. Graph v1.0 has no list views (a beta-only `listView`, ING-11); the Portal setup script that creates them is declared in `graph_families.json` `templates`. |
  | `calendar_meetings` | event, onlineMeeting, callTranscript | Calendars and transcripts read by the agent tooling. |
  | `personal_tasks` | todoTaskList, todoTask, plannerTask | Tasks the agent tooling writes; Mission Control is the task system of record. |

  Every action and `$ref` is a write capability, never data: those entries cite
  `2026-10-07-rec03-production-writes.md` instead of this record.
- decided: the entries stay listed in `source_registry.py gaps` with reason
  `not_business_data` and this record (gap-abd7eb90c7f8: 51 entries at bb601a96, 52 with list
  views), covered by this record. They are not copied into `ontology/source_scope.json`: the
  generator already classifies them by resource type, a list of entry ids there would be a
  second hand-kept copy, and a `microsoft-graph.endpoint` scope would also take the bound
  endpoints out of scope.
- reversal: a use case that needs a meeting, a calendar entry or a directory object as evidence
  turns that resource into a capture and a source family with a live check (a rule with
  `source` in `graph_families.json`), and its entries leave this gap.
- status: decided
