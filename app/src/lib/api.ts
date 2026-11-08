// The one client for operator_api (operator_api/openapi.json). Same-origin
// /api/* (Caddy in production, a Next rewrite in development). Identity is a
// Microsoft access token (Authorization: Bearer, via MSAL) in Entra mode, or
// the HttpOnly session cookie that /api/auth/dev-login sets in dev/mock mode.

import { ApiError, apiErrorFrom } from "./errors";
import { SseParser } from "./sse";
import type { Explore, LabourPage, LateList, RecordAsOf, RecordHistory,
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
  BriefingSeen,
  BriefingView,
  Brief,
  RecordPack,
  MeetingFollowUpResponse,
  MyActionItemsResponse,
  ApprovalsQueue,
  LabelChanges,
  ReplyTriage,
  Workload,
  OwnerWorkload,
  RdBoard,
  UpcomingMeetings,
  WatcherItemState,
  WatcherItemStateRequest,
  DevLoginResponse,
  FeedbackEntry,
  FeedbackList,
  FeedbackRequest,
  FeedbackResponse,
  FeedbackStatus,
  ItemFeedbackRequest,
  NoteFeedbackRequest,
  HomeResponse,
  Me,
  ObjectPage,
  ObjectSection,
  SectionName,
  SourceView,
  SuggestResponse,
  Suggestions,
  ImpactPanel,
  Watchlist,
  WatchFollow,
  WatchRule,
  WatchRuleDraft,
  WatchRuleDeleted,
  WatchReadResult,
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

async function call(path: string, init: RequestInit & { json?: unknown } = {}): Promise<Response> {
  const { json, ...rest } = init;
  const res = await fetch(path, {
    credentials: "same-origin",
    ...rest,
    headers: await headers({
      Accept: "application/json",
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
    }),
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (!res.ok) throw await apiErrorFrom(res);
  return res;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await (await call(path, { signal })).json()) as T;
}

export function getAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  return getJson<AuthConfig>("/api/auth/config", signal);
}

export function getMe(signal?: AbortSignal): Promise<Me> {
  return getJson<Me>("/api/me", signal);
}

export async function devLogin(ticket?: string): Promise<DevLoginResponse> {
  const t = ticket?.trim();
  return (await (await call("/api/auth/dev-login", { method: "POST", json: t ? { ticket: t } : {} })).json()) as DevLoginResponse;
}

export async function logout(): Promise<void> {
  await call("/api/auth/logout", { method: "POST" }).catch(() => undefined);
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

export function getHome(signal?: AbortSignal): Promise<HomeResponse> {
  return getJson<HomeResponse>("/api/home", signal);
}

/** Your briefing: the top events routed to you, every other one counted by type, with the digest preview. */
export function getBriefing(signal?: AbortSignal, view: BriefingView = "all"): Promise<Briefing> {
  return getJson<Briefing>(view === "all" ? "/api/briefing" : `/api/briefing?view=${view}`, signal);
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
  return getJson<LateList>(`/api/late?offset=${Math.max(0, Math.floor(offset))}&limit=${n}`, signal);
}

/** What needs you and what to do: ranked suggestions, each with how we know, a draft and the actions it offers. */
export function getSuggestions(signal?: AbortSignal): Promise<Suggestions> {
  return getJson<Suggestions>("/api/suggestions", signal);
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
// Your own "needs my reply" list: only your mailbox and one-to-one chats; drafts are sent
// only through a card's preview and your confirm.
export function getReplies(signal?: AbortSignal): Promise<ReplyTriage> {
  return getJson<ReplyTriage>("/api/replies", signal);
}

// Counts per team; per person only you and the teams the owner granted you.
export function getWorkload(signal?: AbortSignal): Promise<Workload> {
  return getJson<Workload>("/api/workload", signal);
}

// Per-person counts: the owner only (any other reader gets a 403).
export function getOwnerWorkload(signal?: AbortSignal): Promise<OwnerWorkload> {
  return getJson<OwnerWorkload>("/api/owner/workload", signal);
}

// R&D and sample projects per customer, from the threads and records you may open.
export function getRdBoard(signal?: AbortSignal): Promise<RdBoard> {
  return getJson<RdBoard>("/api/rd-projects", signal);
}

// An item's label and artwork changes from your own alert list.
export function getLabelChanges(objectId: string, signal?: AbortSignal): Promise<LabelChanges> {
  return getJson<LabelChanges>(`/api/label-changes/${encodeURIComponent(objectId)}`, signal);
}

export function getApprovals(signal?: AbortSignal): Promise<ApprovalsQueue> {
  return getJson<ApprovalsQueue>("/api/approvals", signal);
}

/** Calendar events in the next 14 days, each with its brief link. */
export function getUpcomingMeetings(signal?: AbortSignal): Promise<UpcomingMeetings> {
  return getJson<UpcomingMeetings>("/api/brief/meetings", signal);
}

/** Your confirmed actions, newest first. */
export function getActionsLog(signal?: AbortSignal): Promise<ActionsLog> {
  return getJson<ActionsLog>("/api/actions/log", signal);
}

export function suggest(q: string, signal?: AbortSignal): Promise<SuggestResponse> {
  return getJson<SuggestResponse>(`/api/suggest?q=${encodeURIComponent(q.slice(0, 100))}&limit=8`, signal);
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
  return getJson<ObjectPage>(`/api/object/${type}/${encodeURIComponent(id)}${quick ? "?quick=1" : ""}`, signal);
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
 * (`mergeSection`): the late check, people, landed cost, how we know and the margin. */
export const PAGE_SECTIONS: readonly SectionName[] = ["late", "people", "landed_cost", "trail", "margin", "knows"];

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
      return { ...page, profitability: s.profitability ?? null };
    case "knows":
      return { ...page, knows: s.knows ?? null };
  }
  return page;
}

export function getSource(handle: string, textOffset = 0, signal?: AbortSignal): Promise<SourceView> {
  if (!handle || handle.length < 3 || handle.length > 2000) return Promise.reject(new ApiError(404, "not_found"));
  const off = Number.isSafeInteger(textOffset) && textOffset > 0 ? `&text_offset=${textOffset}` : "";
  return getJson<SourceView>(`/api/source?handle=${encodeURIComponent(handle)}${off}`, signal);
}

/** The app's version, attached to every piece of feedback (never anything else about the browser). */
export const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION || "0.1.0";

/** Where the reader is: the app path and query, never the host (works the same on every OS). */
export function currentPage(): string | undefined {
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

/**
 * POST /api/ask and yield typed events as they arrive. Unknown event names
 * are dropped; malformed data or a stream that ends without an answer
 * becomes an `error` event so the UI never shows half-parsed content.
 */
export async function* ask(req: AskRequest, signal?: AbortSignal): AsyncGenerator<AskEvent> {
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
      const { value, done } = await reader.read();
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
