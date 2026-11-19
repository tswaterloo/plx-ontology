// The one client for operator_api (operator_api/openapi.json). Same-origin
// /api/* (Caddy in production, a Next rewrite in development). Identity is a
// Microsoft access token (Authorization: Bearer, via MSAL) in Entra mode, or
// the HttpOnly session cookie that /api/auth/dev-login sets in dev/mock mode.

import { ApiError, apiErrorFrom } from "./errors";
import { onSessionReset } from "./session-cache";
import { SseParser } from "./sse";
import { ownPath, setViewingAs, VIEW_AS_HEADER, viewingAs } from "./view-as";
import type { AskChatUpdate, AskChats, DocList, Explore, LabourPage, LateList, RecordAsOf, RecordHistory,
  InsightInfo, InsightList, InsightNewsList, InsightResult, InsightSave, InsightSchedule, MemoryItem, MemoryItems, MemoryPeople, MemorySave, Template, TemplateDocType, TemplateFilled, TemplateList, TemplateProposal, TemplateSave, TemplateSuggestions,
  WriteApprove, WriteExecute, WriteIntentList, WriteIntentView, WritePreview,
  ActionConfirm,
  ActionPreview,
  ActionRequest,
  ActionResult,
  ActionStates,
  ActionsLog,
  AlertPlaybook,
  AskEvent,
  AskRequest,
  AuthConfig,
  Briefing,
  BriefingItems,
  BriefingPages,
  BriefingUnjudged,
  BriefingSeen,
  BriefingView,
  Brief,
  RecordPack,
  AlertKinds,
  MeetingFollowUpResponse,
  MyActionItemsResponse,
  ApprovalsQueue,
  LabelChanges,
  LabelRevisions,
  ReplyTriage,
  MyWork,
  AutopilotArea,
  AutopilotAudit,
  AutopilotDecisionRequest,
  AutopilotItem,
  AutopilotSwitches,
  AutopilotScorecard,
  TrustPage,
  AutopilotCatalog,
  AutopilotDraftKept,
  AutopilotMode,
  AutopilotReplay,
  AutopilotSchedules,
  AutopilotWorkflowDetail,
  AutopilotWorkflowDraft,
  TeamView,
  Workload,
  WorkloadGrants,
  WorkloadGrantSheet,
  TeamPersonDetail,
  UpcomingMeetings,
  WatcherItemState,
  WatcherItemStateRequest,
  WorkLogKind,
  WorkLogOrganise,
  WorkLogPage,
  WorkLogSave,
  WorkLogThread,
  DevLoginResponse,
  FeedbackEntry,
  FeedbackList,
  FeedbackRequest,
  FeedbackResponse,
  FeedbackStatus,
  ItemFeedbackRequest,
  NoteFeedbackRequest,
  HomeResponse,
  SidebarResponse,
  Me,
  ObjectPage,
  ObjectSection,
  SectionName,
  SourceView,
  SourceRecords,
  SuggestResponse,
  BizKind,
  BizPage,
  Suggestions,
  ImpactPanel,
  Watchlist,
  WatchFollow,
  WatchRule,
  WatchRuleDraft,
  WatchRuleDeleted,
  WatchReadResult,
  TimeSaved,
  UploadPreview,
  PreparedWrite,
  EntryHold,
  EntryConfirm,
  DocFillRequest,
  InstructFill,
  ViewAsPeople,
  LearnedTemplates,
} from "./types";

type TokenProvider = () => Promise<string | null>;
let tokenProvider: TokenProvider | null = null;

/** Entra mode installs a provider; dev/mock mode leaves none (cookie session). */
export function setTokenProvider(p: TokenProvider | null) {
  tokenProvider = p;
}

async function headers(extra: Record<string, string>): Promise<Record<string, string>> {
  const h: Record<string, string> = { ...extra };
  if (tokenProvider) {
    const token = await tokenProvider();
    if (token) h.Authorization = `Bearer ${token}`;
  }
  return h;
}

/** The view-as header for a read while the owner views as someone; a write then is refused here. */
function viewAsHeader(path: string, method: string): Record<string, string> {
  const v = viewingAs();
  if (!v || ownPath(path)) return {};
  if (method !== "GET" && method !== "HEAD") throw new ApiError(403, "view_as_refused");
  return { [VIEW_AS_HEADER]: v.key };
}

/** The longest a page read (a GET) waits for the server's answer: past its own 60 s read limit
 * and a slow network, a server that never answers (frozen, restarting) shows a timeout with a
 * retry instead of a page that spins for ever. Writes and the Ask's stream are not cut here. */
export const GET_TIMEOUT_MS = 90_000;

/** The caller's signal, plus a timeout for a read; a read past it fails as ApiError 504 "read_timeout". */
function readSignal(method: string, signal?: AbortSignal | null): { signal?: AbortSignal; timeout?: AbortSignal } {
  if ((method !== "GET" && method !== "HEAD") || typeof AbortSignal === "undefined" || typeof AbortSignal.timeout !== "function") {
    return { signal: signal ?? undefined };
  }
  const timeout = AbortSignal.timeout(GET_TIMEOUT_MS);
  if (!signal) return { signal: timeout, timeout };
  if (typeof AbortSignal.any !== "function") return { signal };
  return { signal: AbortSignal.any([signal, timeout]), timeout };
}

// Requests in flight (each `call` until its answer's headers): a read the page does not wait
// on (the sidebar's late counts and recent questions) starts once they are done, so it never
// queues beside the page's own reads on the server (measured on the private API: a page's
// read alone 0.50 s median, with the sidebar's Home and late reads beside it 0.94 s).
let callsOpen = 0;
const quietWaiters = new Set<() => void>();
function callEnded() {
  callsOpen = Math.max(0, callsOpen - 1);
  if (callsOpen === 0) for (const f of [...quietWaiters]) f();
}

/** Resolves once no request is in flight (looked at after the current render's effects have
 * sent theirs), or after `most` ms at the latest: for a read nobody on the page waits for. */
export function afterPageReads(most = 2500): Promise<void> {
  return new Promise<void>((done) => {
    let t: ReturnType<typeof setTimeout> | undefined;
    const finish = () => {
      quietWaiters.delete(finish);
      if (t !== undefined) clearTimeout(t);
      done();
    };
    setTimeout(() => {
      if (callsOpen === 0) return finish();
      quietWaiters.add(finish);
      t = setTimeout(finish, most);
    }, 0);
  });
}

async function call(path: string, init: RequestInit & { json?: unknown } = {}): Promise<Response> {
  const { json, ...rest } = init;
  const method = (rest.method ?? "GET").toUpperCase();
  const as = viewAsHeader(path, method);
  const limited = readSignal(method, rest.signal);
  let res: Response;
  callsOpen += 1;
  try {
    res = await fetch(path, {
      credentials: "same-origin",
      ...rest,
      signal: limited.signal,
      headers: await headers({
        Accept: "application/json",
        ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
        ...as,
      }),
      body: json !== undefined ? JSON.stringify(json) : rest.body,
    });
  } catch (e) {
    // our own timeout, not the caller's cancel: an honest "took too long", never a hang
    if (limited.timeout?.aborted && !rest.signal?.aborted) throw new ApiError(504, "read_timeout");
    throw e;
  } finally {
    callEnded();
  }
  if (!res.ok) throw await apiErrorFrom(res);
  return res;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await call(path, { signal });
  try {
    return (await res.json()) as T;
  } catch (e) {
    // the read timed out while its body was still arriving
    if (e instanceof DOMException && e.name === "TimeoutError" && !signal?.aborted) throw new ApiError(504, "read_timeout");
    throw e;
  }
}

/** How often a Today list still being read for the first time is asked for again, and for how long at most. */
export const READING_POLL_MS = 1500;
export const READING_MOST_MS = 300_000;

/** The wait before asking again: short at first (a cold page often fills in a second or two),
 * growing to READING_POLL_MS, and to twice that once the read has gone on for a while. */
export function readingDelay(attempt: number, waited: number): number {
  const cap = waited > READING_POLL_MS * 10 ? READING_POLL_MS * 2 : READING_POLL_MS;
  return Math.min(cap, Math.round(400 * 1.6 ** attempt));
}

/** Waits `ms`, or until the tab is shown again when it is hidden (a hidden tab asks nothing). */
function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((done, fail) => {
    if (signal?.aborted) {
      fail(signal.reason ?? new DOMException("aborted", "AbortError"));
      return;
    }
    let t: ReturnType<typeof setTimeout> | undefined;
    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";
    const finish = () => {
      clearTimeout(t);
      signal?.removeEventListener("abort", stop);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", seen);
    };
    const stop = () => {
      finish();
      fail(signal?.reason ?? new DOMException("aborted", "AbortError"));
    };
    const seen = () => {
      if (hidden()) return;
      finish();
      done();
    };
    t = setTimeout(() => {
      if (hidden()) {
        document.addEventListener("visibilitychange", seen);
        return;
      }
      finish();
      done();
    }, ms);
    signal?.addEventListener("abort", stop, { once: true });
  });
}

/** One request in flight per read: callers asking for the same read while it is under way
 * (the shell and the page, a remount) share it. Each caller's abort lets go of only its own
 * wait; the shared request stops when the last caller has gone. */
const inflight = new Map<string, { result: Promise<unknown>; ctl: AbortController; users: number }>();

// A read under way belongs to the person who started it: sign-out or another sign-in stops it,
// so a later caller (the next person, a hover read-ahead) never joins it and gets its answer.
onSessionReset(() => {
  const under = [...inflight.values()];
  inflight.clear();
  for (const e of under) e.ctl.abort();
});

function shared<T>(key: string, run: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) return Promise.reject(signal.reason ?? new DOMException("aborted", "AbortError"));
  let entry = inflight.get(key);
  if (!entry) {
    const ctl = new AbortController();
    const made = { result: run(ctl.signal) as Promise<unknown>, ctl, users: 0 };
    const gone = () => {
      if (inflight.get(key) === made) inflight.delete(key);
    };
    made.result.then(gone, gone);
    inflight.set(key, made);
    entry = made;
  }
  const e = entry;
  e.users += 1;
  return new Promise<T>((resolve, reject) => {
    let left = false;
    const leave = () => {
      if (left) return;
      left = true;
      signal?.removeEventListener("abort", onAbort);
      e.users -= 1;
      if (e.users === 0 && inflight.get(key) === e) {
        inflight.delete(key);
        e.ctl.abort();
      }
    };
    const onAbort = () => {
      leave();
      reject(signal?.reason ?? new DOMException("aborted", "AbortError"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    e.result.then(
      (v) => {
        leave();
        resolve(v as T);
      },
      (err) => {
        leave();
        reject(err);
      },
    );
  });
}

/** Whose read a path is: the same path read while viewing as someone is a different read. */
function readKey(path: string): string {
  return `${viewingAs()?.key ?? ""} ${path}`;
}

/** A Today list (Home, the late list, the briefing, the suggestions, the replies) or a record's page: the server answers
 * at once with `reading: true` (nothing in it) while its first read for you goes on; ask again until
 * the page is there, so the screens that are ready show while the rest fill in. Still reading after
 * READING_MOST_MS: the calm "busy" notice, never the empty placeholder. */
export function getRead<T extends { reading?: boolean | null }>(path: string, signal?: AbortSignal): Promise<T> {
  return shared(readKey(path), (s) => pollRead<T>(path, s), signal);
}

async function pollRead<T extends { reading?: boolean | null }>(path: string, signal: AbortSignal): Promise<T> {
  const start = Date.now();
  const until = start + READING_MOST_MS;
  for (let attempt = 0; ; attempt++) {
    const got = await getJson<T>(path, signal);
    if (!got.reading) return got;
    const now = Date.now();
    if (now >= until) throw new ApiError(503, "busy");
    await pause(Math.min(readingDelay(attempt, now - start), until - now), signal);
  }
}

export function getAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  return getJson<AuthConfig>("/api/auth/config", signal);
}

export function getMe(signal?: AbortSignal): Promise<Me> {
  return getJson<Me>("/api/me", signal);
}

/** Whom the owner may view as (the owner only). */
export function getViewAsPeople(signal?: AbortSignal): Promise<ViewAsPeople> {
  return getJson<ViewAsPeople>("/api/view-as/people", signal);
}

/** Audit the owner starting or stopping a view as someone; never blocks the switch. */
export async function markViewAs(event: "start" | "stop", key: string): Promise<void> {
  await call(`/api/view-as/${event}`, { method: "POST", json: { key } }).catch(() => undefined);
}

export async function devLogin(ticket?: string): Promise<DevLoginResponse> {
  const t = ticket?.trim();
  return (await (await call("/api/auth/dev-login", { method: "POST", json: t ? { ticket: t } : {} })).json()) as DevLoginResponse;
}

export async function logout(): Promise<void> {
  const was = viewingAs();
  setViewingAs(null);
  if (was) await markViewAs("stop", was.key);
  // Throws when the server could not end the session (the cookie may still be valid); a
  // session that is already gone (401) is signed out all the same.
  await call("/api/auth/logout", { method: "POST" }).catch((e) => {
    if (e instanceof ApiError && e.status === 401) return;
    throw e;
  });
}

/** One operator freshness target from the public health route (operating/ops_health.py). */
export interface OpsCheck {
  name: string;
  value: number | boolean | null;
  target: number;
  unit: string;
  state: "ok" | "breach" | "unknown";
  detail: string;
}

export interface OpsHealth {
  status: "ok" | "breach";
  checked_at: string;
  breaches: string[];
  checks: OpsCheck[];
}

/** The API's liveness and, on a served state dir, its freshness targets (public, no reader). */
export async function getOpsHealth(signal?: AbortSignal): Promise<OpsHealth | null> {
  const body = await getJson<{ status: string; ops?: OpsHealth | null }>("/api/health", signal);
  return body.ops ?? null;
}

/** One reliability incident (operating/ops_alarms.py): shown in the app until it recovers, and mailed by
 * level when ops mail is on (operating/ops_notify.py). A paused one is stopped on purpose until `paused.until`. */
export interface OpsIncident {
  key: string;
  line: string;
  opened_at: string;
  last_seen_at?: string;
  closed_at?: string;
  level?: "info" | "warning" | "high" | "critical";
  paused?: { until: string; why: string };
}

/** Whether the alarms mail anyone (off until the host config names a sender), and why not. */
export interface OpsNotify {
  sending: boolean;
  why_not?: string | null;
  recipients?: number;
  heartbeat_on?: string | null;
  pending?: number;
}

export interface OpsAlarms {
  configured: boolean;
  running: boolean;
  checked_at?: string | null;
  every_minutes?: number;
  open: OpsIncident[];
  paused?: OpsIncident[];
  /** A planned maintenance window (operating/ops_alarms_pauses.py): what it stops is paused until `until`;
   * `overrun` once that has passed (then it pages), `unreadable` for a marker nobody can read. */
  maintenance?: { state: "in_window" | "overrun" | "unreadable"; reason?: string; until?: string } | null;
  recovered: OpsIncident[];
  notify?: OpsNotify | null;
  checks: (OpsCheck & { line?: string })[];
  queue?: OpsQueue | null;
}

/** The host job queue's last snapshot (operating/jobq/status.py): written by its worker every pass. */
export interface OpsQueue {
  configured: boolean;
  fresh: boolean;
  written_at?: string | null;
  age_s?: number;
  queued?: number;
  depth?: Record<string, number>;
  last_hour?: Record<string, number>;
  memory?: { available_mb: number | null; promised_mb: number; floor_mb: number; headroom_mb: number | null };
  running?: { id: number; class: string; unit?: string | null; label?: string | null; state: string; mem_mb: number; current_mb?: number | null; running_s?: number | null; waited_s?: number | null }[];
  waiting?: { id: number; class: string; label?: string | null; waiting_s: number; not_before_s?: number; refusals: number; last_refusal?: string | null }[];
  dead?: { id: number; class: string; attempts: number; result?: string | null; last_error?: string | null; finished_at?: string | null }[];
  unmanaged?: { class: string; unit: string; mem_mb: number; current_mb?: number | null }[];
  events?: { topic: string; state: string; n: number; oldest_s?: number | null }[];
}

/** The reliability alarms' last pass: the owner and the Team readers (403 for every other reader). */
export function getOpsAlarms(signal?: AbortSignal): Promise<OpsAlarms> {
  return getJson<OpsAlarms>("/api/ops/status", signal);
}

export function getHome(signal?: AbortSignal): Promise<HomeResponse> {
  return getRead<HomeResponse>("/api/home", signal);
}

/** The sidebar's numbers (late count, the suppliers with the most, recent questions, as-of): small and kept per reader. */
export function getSidebar(signal?: AbortSignal): Promise<SidebarResponse> {
  return getRead<SidebarResponse>("/api/sidebar", signal);
}

/** Your briefing: the top events routed to you, every other one counted by type, with the digest preview. */
export function getBriefing(signal?: AbortSignal, view: BriefingView = "all", withPages = false): Promise<Briefing> {
  // withPages: the first page of each group Home lists past its top items comes in the same answer
  const q = [view === "all" ? "" : `view=${view}`, withPages ? "pages=true" : ""].filter(Boolean).join("&");
  return getRead<Briefing>(q ? `/api/briefing?${q}` : "/api/briefing", signal);
}

/** One event type's items of your briefing, a page at a time (a group's "+N more"). */
export function getBriefingItems(
  eventType: string,
  offset = 0,
  view: BriefingView = "all",
  signal?: AbortSignal,
): Promise<BriefingItems> {
  const q = new URLSearchParams({ event_type: eventType, offset: String(offset), limit: "25", view });
  return getJson<BriefingItems>(`/api/briefing/items?${q}`, signal);
}

/** The first page of several event types' items of your briefing in one request (Home's groups). */
export function getBriefingPages(eventTypes: string[], view: BriefingView = "all", signal?: AbortSignal): Promise<BriefingPages> {
  const q = new URLSearchParams({ limit: "25", view });
  for (const t of eventTypes) q.append("event_type", t);
  return getJson<BriefingPages>(`/api/briefing/pages?${q}`, signal);
}

/** The records one check could not judge that you may read (a data note's list), a page at a time. */
export function getBriefingUnjudged(check: string, reason: string, offset = 0, signal?: AbortSignal): Promise<BriefingUnjudged> {
  const q = new URLSearchParams({ check, reason, offset: String(offset), limit: "50" });
  return getJson<BriefingUnjudged>(`/api/briefing/unjudged?${q}`, signal);
}

/** Marks your briefing seen: the next "since your last visit" starts now. */
export async function markBriefingSeen(): Promise<BriefingSeen> {
  return (await (await call("/api/briefing/seen", { method: "POST" })).json()) as BriefingSeen;
}

/** Dismiss or snooze one item the AI watcher flagged in your briefing ("open" undoes it).
 * Only your own briefing changes; nothing is sent. */
export async function setWatcherItemState(
  itemId: string,
  state: WatcherItemStateRequest["state"],
  until?: string,
): Promise<WatcherItemState> {
  const json: WatcherItemStateRequest = until ? { state, until } : { state };
  const path = `/api/briefing/watcher/${encodeURIComponent(itemId)}`;
  return (await (await call(path, { method: "POST", json })).json()) as WatcherItemState;
}

/** Your follows, alert rules and alerts, as your grant shows them now. */
export function getWatchlist(signal?: AbortSignal): Promise<Watchlist> {
  return getJson<Watchlist>("/api/watch", signal);
}

/** Follow a record you can open (its page's object id), or stop following it. */
export async function setFollowing(objectId: string, on: boolean): Promise<WatchFollow> {
  const res = on
    ? await call("/api/watch/follow", { method: "POST", json: { object_id: objectId } })
    : await call(`/api/watch/follow/${encodeURIComponent(objectId)}`, { method: "DELETE" });
  return (await res.json()) as WatchFollow;
}

/** An alert rule in plain words, compiled and shown back; nothing is kept. */
export async function compileRule(text: string): Promise<WatchRuleDraft> {
  return (await (await call("/api/watch/rules/compile", { method: "POST", json: { text } })).json()) as WatchRuleDraft;
}

/** Keep the very rule you were shown. */
export async function createRule(draft: WatchRuleDraft): Promise<WatchRule> {
  const json = { text: draft.text, spec: draft.spec, digest: draft.digest };
  return (await (await call("/api/watch/rules", { method: "POST", json })).json()) as WatchRule;
}

export async function deleteRule(ruleId: string): Promise<WatchRuleDeleted> {
  return (await (await call(`/api/watch/rules/${encodeURIComponent(ruleId)}`, { method: "DELETE" })).json()) as WatchRuleDeleted;
}

export async function markAlertsRead(alertIds: string[]): Promise<WatchReadResult> {
  return (await (await call("/api/watch/alerts/read", { method: "POST", json: { alert_ids: alertIds } })).json()) as WatchReadResult;
}

/** Every late purchase order you may see, a page at a time, longest late first ("show all"). */
export function getLateList(offset = 0, signal?: AbortSignal, limit = 25): Promise<LateList> {
  const n = Math.min(100, Math.max(1, Math.floor(limit)));
  return getRead<LateList>(`/api/late?offset=${Math.max(0, Math.floor(offset))}&limit=${n}`, signal);
}

/** What needs you and what to do: ranked suggestions, each with how we know, a draft and the actions it offers. */
export function getSuggestions(signal?: AbortSignal): Promise<Suggestions> {
  return getRead<Suggestions>("/api/suggestions", signal);
}

/** One card action as it would be done: the exact payload and request, its checks; nothing is done. */
export async function previewAction(req: ActionRequest, signal?: AbortSignal): Promise<ActionPreview> {
  return (await (await call("/api/actions/preview", { method: "POST", json: req, signal })).json()) as ActionPreview;
}

/** Do the previewed action as yourself, once per idempotency key (a repeat returns the first result). */
export async function confirmAction(req: ActionConfirm): Promise<ActionResult> {
  return (await (await call("/api/actions/confirm", { method: "POST", json: req })).json()) as ActionResult;
}

/** The write entries you prepared or may approve (approve-to-write). */
export function getWrites(signal?: AbortSignal): Promise<WriteIntentList> {
  return getJson<WriteIntentList>("/api/writes", signal);
}

/** One write entry field by field against the record as read now; nothing is done. */
export function previewWrite(intentId: string, signal?: AbortSignal): Promise<WritePreview> {
  return getJson<WritePreview>(`/api/writes/${encodeURIComponent(intentId)}/preview`, signal);
}

/** Approve the previewed entry as yourself (the second click of a two-step confirm). */
export async function approveWrite(intentId: string, req: WriteApprove): Promise<WriteIntentView> {
  return (await (await call(`/api/writes/${encodeURIComponent(intentId)}/approve`, { method: "POST", json: req })).json()) as WriteIntentView;
}

/** Run an approved entry once: today recorded as "would write" (live writing is off). */
export async function executeWrite(intentId: string, req: WriteExecute): Promise<WriteIntentView> {
  return (await (await call(`/api/writes/${encodeURIComponent(intentId)}/execute`, { method: "POST", json: req })).json()) as WriteIntentView;
}

/** Each of your cards' latest action by you. */
export function getActionStates(signal?: AbortSignal): Promise<ActionStates> {
  return getJson<ActionStates>("/api/actions/state", signal);
}

/** One alert's ready-to-use next steps (drafts, links, the change to make in the source system). */
export function getAlertPlaybook(alertId: string, signal?: AbortSignal): Promise<AlertPlaybook> {
  return getJson<AlertPlaybook>(`/api/alerts/${encodeURIComponent(alertId)}/playbook`, signal);
}

/** The record types a meeting prep brief is prepared for (any other fails closed, no request). */
export const BRIEF_TYPES: readonly string[] = ["CalendarEvent", "Meeting", "Customer", "Supplier"];

/** The meeting prep brief of a calendar event, meeting, customer or supplier. */
export function getMeetingBrief(type: string, id: string, signal?: AbortSignal): Promise<Brief> {
  return getJson<Brief>(`/api/brief/${encodeURIComponent(type)}/${encodeURIComponent(id)}`, signal);
}

/** Department and week labour hours (hr / ops-management readers only). */
export function getLabour(signal?: AbortSignal): Promise<LabourPage> {
  return getJson<LabourPage>("/api/labour", signal);
}

/** The weekly review of the week holding `week` (YYYY-MM-DD; default last week). */
export function getWeeklyReview(week?: string, signal?: AbortSignal): Promise<Brief> {
  return getJson<Brief>(`/api/brief/weekly${week ? `?week=${encodeURIComponent(week)}` : ""}`, signal);
}

/** The record types a document pack is made for (``operating.document_pack.EXPECTED``). */
export const PACK_TYPES = ["SalesOrder", "CustomerPurchaseOrder", "SalesInvoice", "Shipment", "PurchaseOrder", "SupplierInvoice", "Receipt", "Lot", "Batch"];

/** The documents a party usually asks for about one record, with a drafted reply (never sent here). */
export function getRecordPack(type: string, id: string, signal?: AbortSignal): Promise<RecordPack> {
  return getJson<RecordPack>(`/api/record-pack/${encodeURIComponent(type)}/${encodeURIComponent(id)}`, signal);
}

/** A recorded meeting's follow-up (only for its organiser and attendees). */
export function getMeetingFollowUp(meetingId: string, signal?: AbortSignal): Promise<MeetingFollowUpResponse> {
  return getJson<MeetingFollowUpResponse>(`/api/meetings/follow-up/${encodeURIComponent(meetingId)}`, signal);
}

/** My action items from recorded meetings (or one person's, for meetings I organised). */
export function getMyActionItems(person?: string | null, signal?: AbortSignal): Promise<MyActionItemsResponse> {
  const q = person ? `?person=${encodeURIComponent(person)}` : "";
  return getJson<MyActionItemsResponse>(`/api/meetings/action-items${q}`, signal);
}

/** Every pending approval on your alert list, by who it waits on, with a drafted nudge each. */
/** The alert registry: each alert type's measured precision, kind and default. */
export function getAlertKinds(signal?: AbortSignal): Promise<AlertKinds> {
  return getJson<AlertKinds>("/api/alert-kinds", signal);
}

// Your own "needs my reply" list: only your mailbox and one-to-one chats; drafts are sent
// only through a card's preview and your confirm.
export function getReplies(signal?: AbortSignal): Promise<ReplyTriage> {
  return getRead<ReplyTriage>("/api/replies", signal);
}

// The work the autopilots prepared for you (and your granted reports), with your digest.
export function getMyWork(signal?: AbortSignal): Promise<MyWork> {
  return getJson<MyWork>("/api/my-work", signal);
}

/** Your item's prepared entry as a dry-run write intent, approved in the write panel. */
export async function prepareMyWorkWrite(itemId: string): Promise<WriteIntentView> {
  return (await (await call(`/api/my-work/${encodeURIComponent(itemId)}/write`, { method: "POST" })).json()) as WriteIntentView;
}

/** The autopilot area: prepared items you may see, per workflow scores, state and switches. */
export function getAutopilot(signal?: AbortSignal): Promise<AutopilotArea> {
  return getJson<AutopilotArea>("/api/autopilot", signal);
}

/** One thing prepared for you: who wrote, what they wrote, and the reply or the question. */
export type PreparedItem = {
  item_id: string; sender: string; channel: "mail" | "teams" | "system"; subject: string; message: string;
  kind: "reply" | "your_call" | "job" | "todo"; to: string[]; cc: string[]; body: string; edited: boolean;
  /** On a job or a to-do: what the event asks to be done, and each system change proposed for it. */
  summary?: string; jobs?: ProposedJob[];
  /** Things to do by hand that no prepared change reaches, on any kind of item. */
  todos?: TodoStep[];
  ask: string; facts: string[]; options?: { label: string; reply: string }[]; created_at: string;
  /** On a draft you edited: what the edit changed, as it is learned for your next drafts. */
  learned?: string[];
  /** Your preferences and memory the draft applied (DraftUse). */
  used_for_draft?: { memory?: { id: number; kind?: string | null; text?: string | null }[]; related?: { id: number; kind?: string | null; text?: string | null }[]; template?: { id: number; name?: string | null; doc_type?: string | null } | null } | null;
};

/** Something to do by hand: which system, which record, what to do (nothing is written). */
export type TodoStep = { system: string; record: string; what: string };

/** One system change an autopilot proposed (nothing prepared or written until you review it). */
export type ProposedJob = {
  index: number; tool: string; system: string; operation: string; reason: string;
  target: Record<string, unknown>; change: Record<string, unknown>;
  prepared_id?: string | null; state?: string | null;
};

/** Review a proposed change: it is prepared as you (read from its system); confirm it on the card. */
export async function prepareAutopilotJob(itemId: string, index: number): Promise<PreparedWrite> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/jobs/${index}/prepare`, { method: "POST" })).json()) as PreparedWrite;
}

/** Your own Teams sign-in, so a Teams reply you confirm posts as you (never the token). */
export type TeamsConnection = { connected: boolean; scopes?: string[]; connected_at?: string | null; used_at?: string | null; missing?: string[] };

export function getTeamsConnection(signal?: AbortSignal): Promise<TeamsConnection> {
  return getJson<TeamsConnection>("/api/teams/connection", signal);
}

/** Start your one-time Teams sign-in: the Microsoft address to open. */
export async function startTeamsConnect(): Promise<{ url: string }> {
  return (await (await call("/api/teams/connect", { method: "POST" })).json()) as { url: string };
}

export async function forgetTeams(): Promise<TeamsConnection> {
  return (await (await call("/api/teams/connection", { method: "DELETE" })).json()) as TeamsConnection;
}

/** Your open prepared replies, newest first (the one list Home reads too). */
export async function getAutopilotDrafts(signal?: AbortSignal): Promise<{ items: PreparedItem[] }> {
  return getJson<{ items: PreparedItem[] }>("/api/autopilot/drafts", signal);
}

/** The reply to a "your call" card, written from your one line and kept as your edit. */
export async function replyFromLine(itemId: string, line: string): Promise<AutopilotItem> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/reply-from-line`, { method: "POST", json: { line } })).json()) as AutopilotItem;
}

/** Accept, edit, reject or snooze an item of yours (accept: a recorded dry run unless live). */
export async function decideAutopilotItem(itemId: string, body: AutopilotDecisionRequest): Promise<AutopilotItem> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/decision`, { method: "POST", json: body })).json()) as AutopilotItem;
}

/** An order or PO you prepared, held until its checks are settled. */
export function getEntryHold(entryId: string, signal?: AbortSignal): Promise<EntryHold> {
  return getJson<EntryHold>(`/api/entries/${encodeURIComponent(entryId)}`, signal);
}

/** Enter or confirm one value of your held entry. Nothing is written to BC: with no check
 * left the entry goes to the write desk for a colleague to approve. */
export async function confirmEntryValue(entryId: string, body: EntryConfirm): Promise<EntryHold> {
  return (await (await call(`/api/entries/${encodeURIComponent(entryId)}/confirm`, { method: "POST", json: body })).json()) as EntryHold;
}

/** A change Ask or your autopilot prepared: before and after, and what confirming does now. */
export function getPrepared(preparedId: string, signal?: AbortSignal): Promise<PreparedWrite> {
  return getJson<PreparedWrite>(`/api/prepared/${encodeURIComponent(preparedId)}`, signal);
}

/** Your second, Confirm click on a prepared change you previewed; dismiss and undo too. */
export async function preparedStep(
  preparedId: string,
  step: "confirm" | "dismiss" | "undo" | "retry",
  previewHash?: string,
): Promise<PreparedWrite> {
  const path = `/api/prepared/${encodeURIComponent(preparedId)}/${step}`;
  const init = step === "confirm" ? { method: "POST", json: { confirm: true, preview_hash: previewHash } } : { method: "POST" };
  return (await (await call(path, init)).json()) as PreparedWrite;
}

/** A drafted document's upload to SharePoint: before and after, re-checked as you now. */
export function getUpload(uploadId: string, signal?: AbortSignal): Promise<UploadPreview> {
  return getJson<UploadPreview>(`/api/instruct/uploads/${encodeURIComponent(uploadId)}`, signal);
}

/** Approve the upload you previewed (your second, Confirm click). */
export async function approveUpload(uploadId: string, previewHash: string): Promise<UploadPreview> {
  return (await (await call(`/api/instruct/uploads/${encodeURIComponent(uploadId)}/approve`, {
    method: "POST",
    json: { confirm: true, preview_hash: previewHash },
  })).json()) as UploadPreview;
}

/** Fill a form for a record: a new file and every blank with its source, to review. */
export async function fillForm(body: DocFillRequest): Promise<InstructFill> {
  return (await (await call("/api/doc-fill", { method: "POST", json: body })).json()) as InstructFill;
}

/** Switch one of your own autopilot workflows on or off (only your own; on only once it
 * passed its check for you). Answers the area as it now reads. */
export async function setAutopilotOptIn(workflow: string, on: boolean): Promise<AutopilotArea> {
  return (await (await call("/api/autopilot/opt-in", { method: "POST", json: { workflow, on } })).json()) as AutopilotArea;
}

/** Switch one choice of prepared work (drafts and prepared actions waiting in My work for
 * your confirm) on or off for yourself. Answers the area as it now reads. */
export async function setAutopilotDrafts(choice: string, on: boolean): Promise<AutopilotArea> {
  return (await (await call("/api/autopilot/drafts", { method: "POST", json: { choice, on } })).json()) as AutopilotArea;
}

/** Owner only: acknowledge that a workflow passed its check for a role (the owner is told
 * once; it turns nothing on). Answers the area as it now reads. */
export async function ackAutopilotNotice(role: string, workflow: string): Promise<AutopilotArea> {
  return (await (await call("/api/autopilot/notices/ack", { method: "POST", json: { role, workflow } })).json()) as AutopilotArea;
}

/** Pause (or resume) everything you switched on, at once. */
export async function setAutopilotPaused(paused: boolean): Promise<AutopilotArea> {
  return (await (await call("/api/autopilot/pause", { method: "POST", json: { paused } })).json()) as AutopilotArea;
}

/** Owner only: the write path's global kill switch. */
export async function setAutopilotGlobal(globalOff: boolean): Promise<AutopilotSwitches> {
  return (await (await call("/api/autopilot/switches", { method: "POST", json: { global_off: globalOff } })).json()) as AutopilotSwitches;
}

/** One workflow: trigger, conditions, actions, gate, mode, caught and missed, history. */
export function getAutopilotWorkflow(workflow: string, signal?: AbortSignal): Promise<AutopilotWorkflowDetail> {
  return getJson<AutopilotWorkflowDetail>(`/api/autopilot/workflows/${encodeURIComponent(workflow)}`, signal);
}

/** Owner only: a workflow's mode (off, suggest, draft, auto with approval, watch). */
export async function setAutopilotMode(workflow: string, mode: AutopilotMode, role?: string | null): Promise<AutopilotWorkflowDetail> {
  return (await (await call(`/api/autopilot/workflows/${encodeURIComponent(workflow)}/mode`, { method: "PUT", json: { mode, role: role ?? null } })).json()) as AutopilotWorkflowDetail;
}

/** The triggers, conditions and actions the builder offers. */
export function getAutopilotCatalog(signal?: AbortSignal): Promise<AutopilotCatalog> {
  return getJson<AutopilotCatalog>("/api/autopilot/catalog", signal);
}

/** Owner only: keep a drafted workflow (it starts in Draft mode, never live). */
export async function draftAutopilotWorkflow(body: AutopilotWorkflowDraft): Promise<AutopilotDraftKept> {
  return (await (await call("/api/autopilot/workflows", { method: "POST", json: body })).json()) as AutopilotDraftKept;
}

/** Owner only: try a workflow on past days. Nothing is sent or written. */
export async function replayAutopilotWorkflow(workflow: string, days: number): Promise<AutopilotReplay> {
  return (await (await call(`/api/autopilot/workflows/${encodeURIComponent(workflow)}/replay`, { method: "POST", json: { days } })).json()) as AutopilotReplay;
}

/** The workflows that run on a schedule. */
export function getAutopilotSchedules(signal?: AbortSignal): Promise<AutopilotSchedules> {
  return getJson<AutopilotSchedules>("/api/autopilot/schedules", signal);
}

/** Owner only: the write path's audit log, newest first. */
export function getAutopilotAudit(signal?: AbortSignal): Promise<AutopilotAudit> {
  return getJson<AutopilotAudit>("/api/autopilot/audit", signal);
}

/** Owner only: undo a live action once through its recorded inverse. */
export async function undoAutopilotAction(key: string): Promise<AutopilotAudit> {
  return (await (await call(`/api/autopilot/audit/${encodeURIComponent(key)}/undo`, { method: "POST" })).json()) as AutopilotAudit;
}

// The Team view: everyone for the owner and the named Team readers, else only your own row.
export function getTeam(signal?: AbortSignal): Promise<TeamView> {
  return getJson<TeamView>("/api/team", signal);
}

/** Open threads awaiting reply, tasks and alerts per team; per person your own and the teams the owner granted you. */
export function getWorkload(signal?: AbortSignal): Promise<Workload> {
  return getJson<Workload>("/api/workload", signal);
}

/** The owner's grant sheet: each person's team and the teams they see per person (403 for everyone else). */
export function getWorkloadGrants(signal?: AbortSignal): Promise<WorkloadGrantSheet> {
  return getJson<WorkloadGrantSheet>("/api/workload/grants", signal);
}

/** The owner saves the whole assignment (entries for people the sheet never shows are kept by the server). */
export async function setWorkloadGrants(body: WorkloadGrants): Promise<WorkloadGrants> {
  return (await (await call("/api/workload/grants", { method: "POST", json: body })).json()) as WorkloadGrants;
}

// One person's drill-down (404 for a person you may not open).
export function getTeamPerson(key: string, signal?: AbortSignal): Promise<TeamPersonDetail> {
  return getJson<TeamPersonDetail>(`/api/team/${encodeURIComponent(key)}`, signal);
}


// An item's label and artwork changes from your own alert list.
export function getLabelChanges(objectId: string, signal?: AbortSignal): Promise<LabelChanges> {
  return getJson<LabelChanges>(`/api/label-changes/${encodeURIComponent(objectId)}`, signal);
}

export function getLabelRevisions(objectId: string, signal?: AbortSignal): Promise<LabelRevisions> {
  return getJson<LabelRevisions>(`/api/label-revisions/${encodeURIComponent(objectId)}`, signal);
}

export function getApprovals(signal?: AbortSignal): Promise<ApprovalsQueue> {
  return getRead<ApprovalsQueue>("/api/approvals", signal);
}

/** Calendar events in the next 14 days, each with its brief link. */
export function getUpcomingMeetings(signal?: AbortSignal): Promise<UpcomingMeetings> {
  return getRead<UpcomingMeetings>("/api/brief/meetings", signal);
}

/** Your confirmed actions, newest first. */
export function getActionsLog(signal?: AbortSignal): Promise<ActionsLog> {
  return getJson<ActionsLog>("/api/actions/log", signal);
}

export function suggest(q: string, signal?: AbortSignal, limit = 8): Promise<SuggestResponse> {
  const n = Math.max(1, Math.min(20, Math.floor(limit)));
  return getJson<SuggestResponse>(`/api/suggest?q=${encodeURIComponent(q.slice(0, 100))}&limit=${n}`, signal);
}

/** A business record page (customer, chain, supplier account; PO or customer order). */
export function bizPage(kind: BizKind, key: { h?: string; chain?: string; record?: string }, signal?: AbortSignal): Promise<BizPage> {
  const q = new URLSearchParams({ kind });
  if (key.h) q.set("h", key.h.slice(0, 600));
  if (key.chain) q.set("chain", key.chain.slice(0, 200));
  // a record id: the page of the row it was read into (404 when the door has none)
  if (key.record && !key.h) q.set("record", key.record.slice(0, 700));
  return getJson<BizPage>(`/api/biz/page?${q.toString()}`, signal);
}

/** Projection type names (PurchaseOrder, SalesOrder, ...). Anything else fails closed. */
const OBJECT_TYPE = /^[A-Za-z][A-Za-z0-9_]{0,59}$/;

export function isObjectType(t: string): boolean {
  return OBJECT_TYPE.test(t);
}

/** A record's page; `quick`: a first read without the checks (`complete` false) to show
 * while the full page is read. */
export function getObject(type: string, id: string, signal?: AbortSignal, quick = false): Promise<ObjectPage> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  return getRead<ObjectPage>(`/api/object/${type}/${encodeURIComponent(id)}${quick ? "?quick=1" : ""}`, signal);
}

/** A record's core read: its own details only (`core`, status "reading"), served before the
 * quick read while its status, timeline, links and documents are read. */
export function getObjectCore(type: string, id: string, signal?: AbortSignal): Promise<ObjectPage> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  return getJson<ObjectPage>(`/api/object/${type}/${encodeURIComponent(id)}?quick=1&core=1`, signal);
}

/** A record's neighbours one hop out, by kind of link, as this reader may see them (the
 * link explorer). Walking on is this call for a neighbour. */
export function getExplore(type: string, id: string, signal?: AbortSignal): Promise<Explore> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  return getJson<Explore>(`/api/explore/${type}/${encodeURIComponent(id)}`, signal);
}

/** A record's change log, newest first, a page at a time (`cursor`: the page before's
 * `next_cursor`), as this reader may read it. */
export function getHistory(type: string, id: string, cursor?: string | null, signal?: AbortSignal): Promise<RecordHistory> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  const qs = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return getJson<RecordHistory>(`/api/history/${type}/${encodeURIComponent(id)}${qs}`, signal);
}

/** The record as recorded at a date (its end) or an ISO time with zone. */
export function getAsOf(type: string, id: string, at: string, signal?: AbortSignal): Promise<RecordAsOf> {
  if (!isObjectType(type) || !id || id.length > 700 || !/^\d{4}-\d{2}-\d{2}/.test(at)) return Promise.reject(new ApiError(404, "not_found"));
  return getJson<RecordAsOf>(`/api/as-of/${type}/${encodeURIComponent(id)}?at=${encodeURIComponent(at)}`, signal);
}

/** The slow sections of a record's page, read after its quick first read and merged in
 * (`mergeSection`): the late check, people, landed cost, how we know, the margin, what a
 * person works on, the lists from customers' and suppliers' sheets and mail and a product's formula. */
export const PAGE_SECTIONS: readonly SectionName[] = ["late", "people", "landed_cost", "trail", "margin", "knows", "documents", "formula"];
/** The record types whose page has the documents section (`operator_api.record_documents.LISTS`). */
export const DOCUMENT_LIST_TYPES: readonly string[] = ["Customer", "Supplier", "Product", "FgItem"];

export function getObjectSection(section: SectionName, type: string, id: string, signal?: AbortSignal): Promise<ObjectSection> {
  if (!PAGE_SECTIONS.includes(section) || !isObjectType(type) || !id || id.length > 700)
    return Promise.reject(new ApiError(404, "not_found"));
  return getJson<ObjectSection>(`/api/object-section/${section}/${type}/${encodeURIComponent(id)}`, signal);
}

export type ImpactChange = { kind: "delay"; days: number } | { kind: "out_of_stock" } | { kind: "customer_lost" } | { kind: "price_change"; percent: string };

/** A record's Impact panel, read on demand: what-if (a change walked downstream) or why. */
export function getImpact(mode: "what_if" | "why", type: string, id: string, change?: ImpactChange, signal?: AbortSignal): Promise<ImpactPanel> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  const q = new URLSearchParams();
  if (mode === "what_if" && change) {
    q.set("change", change.kind);
    if (change.kind === "delay") q.set("days", String(Math.max(1, Math.min(365, Math.round(change.days)))));
    if (change.kind === "price_change") q.set("percent", change.percent);
  }
  const qs = q.toString();
  return getJson<ImpactPanel>(`/api/object-impact/${mode}/${type}/${encodeURIComponent(id)}${qs ? `?${qs}` : ""}`, signal);
}

/** `page` with one section merged, as the full page (no `quick`) carries it. */
export function mergeSection(page: ObjectPage, s: ObjectSection): ObjectPage {
  if (s.object_id !== page.object_id) return page;
  switch (s.section) {
    case "late":
      return { ...page, ...(s.status ? { status: s.status } : {}), late_lines: s.late_lines ?? [] };
    case "people":
      return { ...page, people: s.people ?? null };
    case "landed_cost":
      return { ...page, landed_cost: s.landed_cost ?? null };
    case "trail":
      return { ...page, trail: s.trail ?? null, ...(s.not_found ? { not_found: s.not_found } : {}), ...(s.proposals ? { proposals: s.proposals } : {}) };
    case "margin":
      return { ...page, profitability: s.profitability ?? null, profitability_choices: s.profitability_choices ?? [] };
    case "knows":
      return { ...page, knows: s.knows ?? null };
    case "documents":
      return { ...page, documents: s.documents ?? null };
    case "formula":
      return { ...page, formula: s.formula ?? null };
  }
  return page;
}

export function getSource(handle: string, textOffset = 0, signal?: AbortSignal): Promise<SourceView> {
  if (!handle || handle.length < 3 || handle.length > 2000) return Promise.reject(new ApiError(404, "not_found"));
  const off = Number.isSafeInteger(textOffset) && textOffset > 0 ? `&text_offset=${textOffset}` : "";
  return getJson<SourceView>(`/api/source?handle=${encodeURIComponent(handle)}${off}`, signal);
}

/** The records citing a source, as this reader may read them (404 when the source isn't theirs to open). */
export function getSourceRecords(handle: string, signal?: AbortSignal): Promise<SourceRecords> {
  if (!handle || handle.length < 3 || handle.length > 2000) return Promise.reject(new ApiError(404, "not_found"));
  return getJson<SourceRecords>(`/api/source/records?handle=${encodeURIComponent(handle)}`, signal);
}

/** The app's version, attached to every piece of feedback (never anything else about the browser). */
export const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION || "0.1.0";

/** Where the reader is: the app path and query, never the host (works the same on every OS). */
function currentPage(): string | undefined {
  if (typeof window === "undefined") return undefined;
  return (window.location.pathname + window.location.search).slice(0, 300);
}

export async function sendFeedback(body: FeedbackRequest): Promise<FeedbackResponse> {
  const json: FeedbackRequest = { page: currentPage(), app_version: APP_VERSION, ...body };
  return (await (await call("/api/feedback", { method: "POST", json })).json()) as FeedbackResponse;
}

/** A briefing or alert item: useful, not useful or wrong. */
export async function sendItemFeedback(body: ItemFeedbackRequest): Promise<FeedbackResponse> {
  const json: ItemFeedbackRequest = { page: currentPage(), app_version: APP_VERSION, ...body };
  return (await (await call("/api/feedback/item", { method: "POST", json })).json()) as FeedbackResponse;
}

/** A note on a page, a record, a list, the app or anything else. */
export async function sendNoteFeedback(body: NoteFeedbackRequest): Promise<FeedbackResponse> {
  const json: NoteFeedbackRequest = { page: currentPage(), app_version: APP_VERSION, ...body };
  return (await (await call("/api/feedback/note", { method: "POST", json })).json()) as FeedbackResponse;
}

export async function getFeedback(
  filters: { kind?: string; page?: string; status?: string; order?: "newest" | "oldest"; offset?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<FeedbackList> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) if (v !== undefined && v !== "") q.set(k, String(v));
  const tail = q.toString();
  return (await (await call(`/api/feedback${tail ? `?${tail}` : ""}`, { signal })).json()) as FeedbackList;
}

/** Triage (the owner and the admins only). */
export async function setFeedbackStatus(id: string, status: FeedbackStatus): Promise<FeedbackEntry> {
  return (await (await call(`/api/feedback/${encodeURIComponent(id)}/status`, { method: "POST", json: { status } })).json()) as FeedbackEntry;
}

const ASK_EVENTS = new Set(["understanding", "status", "sources", "verdict", "token", "answer", "error"]);

/** How long the Ask's stream may say nothing at all before it counts as cut off: the server
 * sends a keep-alive every 15 s while an answer is still being worked on (operator_api.app
 * KEEP_ALIVE_SECONDS), so only a frozen or restarted server goes this long. */
export const ASK_STALL_MS = 60_000;

/** The reader's next chunk, or "stalled" when none (not even a keep-alive) came within `ms`. */
async function readOrStall<T>(reader: ReadableStreamDefaultReader<T>, ms: number): Promise<ReadableStreamReadResult<T> | "stalled"> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([reader.read(), new Promise<"stalled">((done) => (timer = setTimeout(() => done("stalled"), ms)))]);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * POST /api/ask and yield typed events as they arrive. Unknown event names
 * are dropped; malformed data or a stream that ends without an answer
 * becomes an `error` event so the UI never shows half-parsed content.
 */
export async function* ask(req: AskRequest, signal?: AbortSignal): AsyncGenerator<AskEvent> {
  viewAsHeader("/api/ask", "POST");
  const res = await fetch("/api/ask", {
    method: "POST",
    credentials: "same-origin",
    headers: await headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok) throw await apiErrorFrom(res);
  if (!res.body) throw new ApiError(res.status, "stream_interrupted");

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  let finished = false;
  try {
    for (;;) {
      const next = await readOrStall(reader, ASK_STALL_MS);
      if (next === "stalled") {
        // nothing at all (not even the server's keep-alive) for a minute: the server froze
        // or restarted under the answer; say it was cut off rather than wait for ever
        void reader.cancel().catch(() => undefined);
        yield { event: "error", data: { code: "stream_interrupted", message: "" } };
        return;
      }
      const { value, done } = next;
      const raws = done ? parser.end() : parser.push(value);
      for (const raw of raws) {
        if (!ASK_EVENTS.has(raw.event)) continue;
        let data: unknown;
        try {
          data = JSON.parse(raw.data);
        } catch {
          yield { event: "error", data: { code: "stream_interrupted", message: "" } };
          return;
        }
        if (!data || typeof data !== "object") {
          yield { event: "error", data: { code: "stream_interrupted", message: "" } };
          return;
        }
        if (raw.event === "answer" || raw.event === "error") finished = true;
        yield { event: raw.event, data } as AskEvent;
        if (finished) return;
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
  if (!finished) yield { event: "error", data: { code: "stream_interrupted", message: "" } };
}

// ---- the work log (operator_api.work_log): everything you did or received, in one place ----

export interface WorkLogFilters {
  q?: string;
  kind?: WorkLogKind;
  record?: string;
  thread?: string;
  case_id?: string;
  pinned?: boolean;
  waiting?: boolean;
  everyone?: boolean;
  offset?: number;
  limit?: number;
}

/** Your work log, newest first (the owner may ask for everyone's). */
export function getWorkLog(filters: WorkLogFilters = {}, signal?: AbortSignal): Promise<WorkLogPage> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) if (v !== undefined && v !== "" && v !== false) q.set(k, String(v));
  const tail = q.toString();
  return getJson<WorkLogPage>(`/api/work-log${tail ? `?${tail}` : ""}`, signal);
}

/** The time the Workbench saved you each week (the owner may ask for everyone's): an estimate with its method. */
/** Your autopilot scorecard per day: prepared, accepted, edited, rejected. */
export function getAutopilotDays(days = 14, signal?: AbortSignal): Promise<AutopilotScorecard> {
  return getJson<AutopilotScorecard>(`/api/work-log/autopilot-days?days=${days}`, signal);
}

/** The Trust page: freshness, answer scores, alert precision, autopilot, switches. */
export function getTrust(signal?: AbortSignal): Promise<TrustPage> {
  return getJson<TrustPage>("/api/trust", signal);
}

export function getTimeSaved(everyone = false, signal?: AbortSignal): Promise<TimeSaved> {
  return getJson<TimeSaved>(`/api/time-saved${everyone ? "?everyone=true" : ""}`, signal);
}

const threadPath = (thread: string) => `/api/work-log/threads/${thread.split("/").map(encodeURIComponent).join("/")}`;

/** One of your threads and what continuing it reopens. */
export function getWorkThread(thread: string, signal?: AbortSignal): Promise<WorkLogThread> {
  return getJson<WorkLogThread>(threadPath(thread), signal);
}

/** Your Ask conversations, pinned first then the most recent; ``q`` searches their names and questions. */
export function getAskChats(q?: string, signal?: AbortSignal): Promise<AskChats> {
  return getJson<AskChats>(`/api/ask/chats${q && q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ""}`, signal);
}

/** Rename, pin or delete one of your Ask conversations; your chats list after. */
export async function updateAskChat(conversationId: string, body: AskChatUpdate): Promise<AskChats> {
  return (await (await call(`/api/ask/chats/${encodeURIComponent(conversationId)}`, { method: "POST", json: body })).json()) as AskChats;
}

/** Name, pin, attach to a case or close one of your threads. */
export async function organiseWorkThread(thread: string, body: WorkLogOrganise): Promise<WorkLogThread> {
  return (await (await call(threadPath(thread), { method: "POST", json: body })).json()) as WorkLogThread;
}

/** A Save or Unsave, kept in your work log as well as this browser. */
export async function logSaved(body: WorkLogSave): Promise<void> {
  await call("/api/work-log/saved", { method: "POST", json: body });
}

// ---- memory: what the Workbench remembers about you (yours only) ----

/** Your standing instructions, preferences, commitments and facts, newest first. */
export function getMemory(signal?: AbortSignal): Promise<MemoryItems> {
  return getJson<MemoryItems>("/api/memory", signal);
}

/** The people you work with most and your conversations going, from your own mailbox. */
export function getMemoryPeople(signal?: AbortSignal): Promise<MemoryPeople> {
  return getJson<MemoryPeople>("/api/memory/people", signal);
}

/** Keep something in your own words, or correct one of your items (id). */
export async function saveMemory(body: MemorySave, id?: number): Promise<MemoryItem> {
  const res = id
    ? await call(`/api/memory/${id}`, { method: "PUT", json: body })
    : await call("/api/memory", { method: "POST", json: body });
  return (await res.json()) as MemoryItem;
}

/** One of your commitments done: it leaves Today and your memory; your log keeps it. */
export async function doneMemory(id: number): Promise<void> {
  await call(`/api/memory/${id}/done`, { method: "POST" });
}

/** Forget one of your items: it is not used again, and not learned again unseen. */
export async function forgetMemory(id: number): Promise<void> {
  await call(`/api/memory/${id}`, { method: "DELETE" });
}

// ---- insights: your dashboard (built-in team, org and customer views, and what you pinned) ----

/** The insights you may see: the built-ins, then the ones you pinned or saved. */
export function getInsights(signal?: AbortSignal): Promise<InsightList> {
  return getJson<InsightList>("/api/insights", signal);
}

/** One insight as you read it (kept from your read of the last few minutes; `again`: read
 * now): its statement, rows, chart and the people they name. */
export function getInsight(id: string, signal?: AbortSignal, again = false): Promise<InsightResult> {
  return getJson<InsightResult>(`/api/insights/${encodeURIComponent(id)}${again ? "?again=true" : ""}`, signal);
}

/** Pin a chart (or save a statement) to your dashboard; it reads again when opened. */
export async function saveInsight(body: InsightSave): Promise<InsightInfo> {
  const res = await call("/api/insights", { method: "POST", json: body });
  return (await res.json()) as InsightInfo;
}

/** A chart you were shown as a file (Excel with a live chart over its rows, PowerPoint, Word or PDF). */
export async function chartFile(chart: unknown, format: "xlsx" | "pptx" | "docx" | "pdf"): Promise<Blob> {
  const res = await call("/api/charts/file", { method: "POST", json: { chart, format } });
  return res.blob();
}

/** Remove one of your saved insights. */
export async function deleteInsight(id: string): Promise<void> {
  await call(`/api/insights/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** When one of your saved insights runs by itself (as you) and when what it finds comes to you on Home. */
export async function setInsightSchedule(id: string, body: InsightSchedule): Promise<InsightInfo> {
  const res = await call(`/api/insights/${encodeURIComponent(id)}/schedule`, { method: "PUT", json: body });
  return (await res.json()) as InsightInfo;
}

/** What your scheduled insights found that came to you (counts only; open one to read it live). */
export function getInsightNews(signal?: AbortSignal): Promise<InsightNewsList> {
  return getJson<InsightNewsList>("/api/insight-news", signal);
}

/** Put one away: it leaves Home. */
export async function putAwayInsightNews(runId: string): Promise<void> {
  await call(`/api/insight-news/${encodeURIComponent(runId)}/seen`, { method: "POST" });
}

// ---- templates: your own saved templates, and those shared with you ----

/** Your templates and those shared with you or a role of yours, with the types, fields and roles. */
export function getTemplates(signal?: AbortSignal, docType?: TemplateDocType): Promise<TemplateList> {
  return getJson<TemplateList>(`/api/templates${docType ? `?doc_type=${encodeURIComponent(docType)}` : ""}`, signal);
}

/** The questionnaire answers we gave that any customer may get, ranked against `question`;
 * answered `reading` while your first read of it goes on, asked again until it is there. */
export function getAnswerBank(question?: string, signal?: AbortSignal): Promise<DocList> {
  const q = (question ?? "").trim().slice(0, 700);
  return getRead<DocList>(`/api/templates/answer-bank${q ? `?question=${encodeURIComponent(q)}` : ""}`, signal);
}

/** Save a new template, or change one of yours (id). */
export async function saveTemplate(body: TemplateSave, id?: number): Promise<Template> {
  const res = id
    ? await call(`/api/templates/${id}`, { method: "PUT", json: body })
    : await call("/api/templates", { method: "POST", json: body });
  return (await res.json()) as Template;
}

export async function deleteTemplate(id: number): Promise<void> {
  await call(`/api/templates/${id}`, { method: "DELETE" });
}

/** A draft's text with the record's exact values as placeholders; nothing is saved. */
export async function proposeTemplate(text: string, record?: string | null): Promise<TemplateProposal> {
  return (await (await call("/api/templates/propose", { method: "POST", json: { text, record: record || null } })).json()) as TemplateProposal;
}

/** A template filled from a record you can read; nothing is sent. */
export async function fillTemplate(id: number, record?: string | null): Promise<TemplateFilled> {
  return (await (await call(`/api/templates/${id}/fill`, { method: "POST", json: { record: record || null } })).json()) as TemplateFilled;
}

/** The kinds of document learned from your own sent documents (re-learned daily). */
export function getLearnedTemplates(signal?: AbortSignal): Promise<LearnedTemplates> {
  return getJson<LearnedTemplates>("/api/templates/learned", signal);
}

/** The templates to offer when you start a document of this type. */
export function suggestTemplates(docType: TemplateDocType, signal?: AbortSignal): Promise<TemplateSuggestions> {
  return getJson<TemplateSuggestions>(`/api/templates/suggest?doc_type=${encodeURIComponent(docType)}`, signal);
}
