// The one client for operator_api (operator_api/openapi.json). Same-origin
// /api/* (Caddy in production, a Next rewrite in development). Identity is a
// Microsoft access token (Authorization: Bearer, via MSAL) in Entra mode, or
// the HttpOnly session cookie that /api/auth/dev-login sets in dev/mock mode.

import { ApiError, apiErrorFrom, incidentOf } from "./errors";
import { quiet } from "./quiet";
import { writeEpoch } from "./use-read";
import { DOOR_HEADER, forgetAnswer, isSettledAnswer, noteAnswerHeaders, noteOwnWrite, rememberAnswer } from "./read-store";
import { onSessionReset } from "./session-cache";
import { SseParser } from "./sse";
import { ownPath, setViewingAs, VIEW_AS_HEADER, viewingAs } from "./view-as";
import type { AskChatUpdate, AskChats, DataHealth, RecordList, DocList, RecordViews, Explore, LabourPage, LateList, PoClosure, RecordAsOf, RecordHistory,
  AskObserved, EmailDraftPrepared, EmailDraftRequest, ExportXlsxRequest, FilterEdit, GapList, MeasureCandidate, MeasureCandidateApprove, MeasureCandidateDismiss, MeasureCandidateList, MeasureCandidatePropose, MeasureCandidateStatus, StatementRead, InsightInfo, InsightList, InsightNewsList, InsightResult, InsightSave, InsightSchedule, MemoryItem, MemoryItems, MemoryPeople, MemorySave, Template, TemplateDocType, TemplateFilled, TemplateList, TemplateProposal, TemplateSave, TemplateSuggestions,
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
  AutopilotArea,
  AutopilotAudit,
  AutopilotDecisionRequest,
  AutopilotItem,
  AutopilotSwitches,
  AutopilotScorecard,
  TrustPage,
  FeedStates,
  AutopilotCatalog,
  AutopilotDraftKept,
  AutopilotMode,
  AutopilotReplay,
  AutopilotSchedules,
  RecurringChange,
  RecurringInstruction,
  RecurringInstructions,
  RecurringKeep,
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
  HomeNeeds,
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

// The page's reads may start before the sign-in check has chosen cookie or Microsoft token
// (W1: nothing waits for /api/me). In Microsoft mode such a read goes without the token and is
// refused (401); it is then sent once more when the check has installed the provider. In
// cookie mode a 401 is the answer, as before.
let authPending = false;
let authSettledP: Promise<void> | null = null;
let authSettle: (() => void) | null = null;
/** The auth layer starts its sign-in check: a 401 before `settleAuth` may be a read sent too early. */
export function beginAuthCheck(): void {
  if (authPending) return;
  authPending = true;
  authSettledP = new Promise<void>((r) => (authSettle = r));
}
/** The auth layer has chosen cookie or token (the provider is set or deliberately absent). */
export function settleAuth(): void {
  authPending = false;
  authSettle?.();
  authSettle = null;
}

async function headers(extra: Record<string, string>): Promise<Record<string, string>> {
  const h: Record<string, string> = { ...extra };
  if (tokenProvider) {
    const token = await tokenProvider();
    if (token) h.Authorization = `Bearer ${token}`;
  }
  return h;
}

/** Headers for the session owner's own stream (never a view-as one): the Microsoft token
 * when Entra mode uses one, else just `extra` and the cookie. */
export function ownerStreamHeaders(extra: Record<string, string>): Promise<Record<string, string>> {
  return headers(extra);
}

/** The view-as header for a read while the owner views as someone; a write then is refused here. */
function viewAsHeader(path: string, method: string): Record<string, string> {
  const v = viewingAs();
  if (!v || ownPath(path)) return {};
  if (method !== "GET" && method !== "HEAD") throw new ApiError(403, "view_as_refused");
  return { [VIEW_AS_HEADER]: v.key };
}

/** The only bound on a request (owner 11-23: no limit on total time): a hung socket. A request
 * aborts only when NO bytes have arrived for this long, counted from the request, then from each
 * chunk of the answer; it fails as ApiError 504 "connection_stalled" (a plain notice with Retry),
 * never as an empty answer. */
export const IDLE_STALL_MS = 120_000;

/** `res` with its body watched: each chunk restarts the idle clock; a stall (the request was
 * aborted by the clock) errors the body as "connection_stalled". */
function watchBody(res: Response, restart: () => void, stop: () => void, wasStalled: () => boolean): Response {
  if (!res.body) {
    stop();
    return res;
  }
  const reader = res.body.getReader();
  const watched = new ReadableStream<Uint8Array>({
    async pull(c) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          stop();
          c.close();
          return;
        }
        restart();
        c.enqueue(value);
      } catch (e) {
        stop();
        c.error(wasStalled() ? new ApiError(504, "connection_stalled") : e);
      }
    },
    cancel(r) {
      stop();
      return reader.cancel(r);
    },
  });
  return new Response(watched, { status: res.status, statusText: res.statusText, headers: res.headers });
}

// Requests in flight (each `call` until its answer's headers): a read the page does not wait
// on (the sidebar's late counts and recent questions) starts once they are done, so it never
// queues beside the page's own reads on the server (measured on the private API: a page's
// read alone 0.50 s median, with the sidebar's Home and late reads beside it 0.94 s).
let callsOpen = 0;
// the same, without read-aheads (W5/W6: a page is complete when its own reads are answered)
let pageCallsOpen = 0;
const callListeners = new Set<() => void>();
/** Reads in flight that the page itself asked for (not read-aheads). */
export function pageReadsOpen(): number {
  return pageCallsOpen;
}
/** Calls `f` when the count of the page's own reads in flight changes; returns the unsubscribe. */
export function onPageReadsChange(f: () => void): () => void {
  callListeners.add(f);
  return () => void callListeners.delete(f);
}
const quietWaiters = new Set<() => void>();
function callEnded(ahead: boolean) {
  if (!ahead) {
    pageCallsOpen = Math.max(0, pageCallsOpen - 1);
    for (const f of [...callListeners]) f();
  }
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

async function call(path: string, init: RequestInit & { json?: unknown; ahead?: boolean } = {}): Promise<Response> {
  const { json, ahead, ...rest } = init;
  const method = (rest.method ?? "GET").toUpperCase();
  if (method !== "GET") {
    autopilotLast = null;
    writesSent += 1;
    paneKept.clear();
    insightsKept.clear();
    recordAhead.clear();
    noteOwnWrite(path);
  }
  const as = viewAsHeader(path, method);
  // the caller's cancel, plus our own abort when nothing has arrived for IDLE_STALL_MS
  const ctl = new AbortController();
  const onCallerAbort = () => ctl.abort(rest.signal?.reason);
  if (rest.signal?.aborted) ctl.abort(rest.signal.reason);
  else rest.signal?.addEventListener("abort", onCallerAbort, { once: true });
  let stalled = false;
  let clock: ReturnType<typeof setTimeout> | undefined;
  // (the caller's abort stays linked after the answer: it still ends a body being read)
  const stop = () => clearTimeout(clock);
  const restart = () => {
    clearTimeout(clock);
    clock = setTimeout(() => {
      stalled = true;
      ctl.abort(new ApiError(504, "connection_stalled"));
    }, IDLE_STALL_MS);
  };
  restart();
  let res: Response;
  callsOpen += 1;
  // a read-ahead, or a re-read behind an answer already drawn (`quietly`), is not the page's own read
  const background = !!ahead || quiet();
  if (!background) {
    pageCallsOpen += 1;
    for (const f of [...callListeners]) f();
  }
  try {
    const send = async () =>
      fetch(path, {
        credentials: "same-origin",
        ...rest,
        signal: ctl.signal,
        headers: await headers({
          Accept: "application/json",
          ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
          ...as,
          ...(ahead ? { "X-PLX-Ahead": "1" } : {}),
        }),
        body: json !== undefined ? JSON.stringify(json) : rest.body,
      });
    const tokenBefore = tokenProvider !== null;
    res = await send();
    if (res.status === 401 && method === "GET" && !tokenBefore && authPending && authSettledP) {
      await authSettledP;
      if (tokenProvider) res = await send();   // sent too early for Microsoft mode: once more, with the token
    }
  } catch (e) {
    stop();
    // nothing arrived for IDLE_STALL_MS (not the caller's cancel): said plainly, never a hang
    if (stalled && !rest.signal?.aborted) throw new ApiError(504, "connection_stalled");
    throw e;
  } finally {
    callEnded(background);
  }
  restart();
  // the door value and any refused-session mark come before the body is used (`read-store.ts`)
  if (path.startsWith("/api/")) noteAnswerHeaders(res.headers);
  if (method !== "GET") noteOwnWrite(path); // a read that finished while the write was out may carry the older answer
  const watched = watchBody(res, restart, stop, () => stalled && !rest.signal?.aborted);
  if (!res.ok) throw await apiErrorFrom(watched);
  return watched;
}

/** A read the shell and a page both ask for on a first visit (/api/me, /api/ops/status):
 * callers asking while it is under way share its one request (`shared`, as the Today lists
 * do); each caller's abort lets go of only its own wait. Asked after it answered, it is read
 * again. Other reads keep their caller's own request (a typed lookup aborts its own). */
function getShared<T>(path: string, signal?: AbortSignal): Promise<T> {
  // its own key: a Today list read by `getRead` polls until it is read, this answers at once
  return shared(`json ${readKey(path)}`, (s) => getJson<T>(path, s), signal);
}

// A record pane's reads (the door page, the evidence trail; ``reuse``): Home's pane is mounted again
// as the lists fill in (the first row picked from the first list that came, then the same
// row from the briefing), and a person clicks back and forth between rows; an answer read
// this recently is reused, the same as the autopilot area's (any write and a sign-out drop
// them, and a second ask while one is under way shares it).
export const PANE_REUSE_MS = 15_000;
const PANE_KEPT_MOST = 24;
const paneKept = new Map<string, { at: number; got: unknown }>();
onSessionReset(() => paneKept.clear());

function getPaneRead<T>(path: string, signal?: AbortSignal): Promise<T> {
  const key = `pane ${readKey(path)}`;
  const hit = paneKept.get(key);
  if (hit && Date.now() - hit.at < PANE_REUSE_MS) return Promise.resolve(hit.got as T);
  const before = writesSent;
  const read = shared(key, (s) => getJson<T>(path, s), signal);
  read.then(
    (got) => {
      if (writesSent !== before) return;   // read across a write: never kept
      // a stale or still-reading answer is asked for again soon: never reused
      if (!isSettledAnswer(got)) return;
      paneKept.delete(key);
      paneKept.set(key, { at: Date.now(), got });
      while (paneKept.size > PANE_KEPT_MOST) paneKept.delete(paneKept.keys().next().value as string);
    },
    () => undefined,
  );
  return read;
}

async function getJson<T>(path: string, signal?: AbortSignal, ahead = false): Promise<T> {
  // the head script read it with the cookie alone (cookie mode, the reader's own view): its
  // answer stands in once; a refused or failed early read is read again through `call`
  if (!ahead && !tokenProvider && !viewingAs()) {
    const early = takeEarly(`r:${path}`, signal);
    if (early) {
      pageCallsOpen += 1;
      for (const f of [...callListeners]) f();
      let got: EarlyAnswer | null = null;
      try {
        got = await early;
      } finally {
        pageCallsOpen = Math.max(0, pageCallsOpen - 1);
        for (const f of [...callListeners]) f();
      }
      if (got?.ok && got.body !== undefined) return got.body as T;
    }
  }
  let res: Response;
  const began = writeEpoch();
  try {
    res = await call(path, { signal, ahead });
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) forgetAnswer(path); // a kept answer for a record that is gone
    throw e;
  }
  const text = await res.text();
  const body = JSON.parse(text) as T;
  if (began === writeEpoch()) rememberAnswer(path, text, res.headers.get(DOOR_HEADER), body); // not an answer read before a change of yours
  return body;
}

/** One GET of an API path, as the page's own read of it (the route table's loader: read-ahead, warm-up).
 * The answer is exactly what the path sends; `ahead` marks a read nobody is waiting on. */
export function readPath<T>(path: string, signal?: AbortSignal, ahead = false): Promise<T> {
  return getJson<T>(path, signal, ahead);
}

/** How often a Today list still being read for the first time is asked for again, and for how long at most. */
export const READING_POLL_MS = 1500;
export const READING_MOST_MS = 300_000;

/** The wait before asking again: short at first (a cold page often fills in a second or two),
 * growing to READING_POLL_MS, to twice that once the read has gone on for a while, and to four
 * times that once it has gone on for half a minute: a read that long is slow on the server, and
 * asking every few seconds only kept the screen's requests going (11-22: Home's record pane
 * re-asked every ~2.5 s for a read that did not finish, so Home never finished loading). */
export function readingDelay(attempt: number, waited: number): number {
  const cap = waited > READING_POLL_MS * 20 ? READING_POLL_MS * 4
    : waited > READING_POLL_MS * 10 ? READING_POLL_MS * 2 : READING_POLL_MS;
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
export function getRead<T extends { reading?: boolean | null }>(path: string, signal?: AbortSignal, ahead = false): Promise<T> {
  return shared(readKey(path), (s) => pollRead<T>(path, s, ahead), signal);
}

async function pollRead<T extends { reading?: boolean | null }>(path: string, signal: AbortSignal, ahead = false): Promise<T> {
  const start = Date.now();
  const until = start + READING_MOST_MS;
  for (let attempt = 0; ; attempt++) {
    // Home's first reads come in one answer (startHomeBundle); a part that is still being
    // read, or no bundle, goes on to the part's own request below
    // (awaited only for a bundled read: an await here would end `quietly`'s scope before the
    // request is sent, and a re-read behind a drawn answer would count as the page's own read)
    const bundled = attempt === 0 && bundleMayAnswer(path) ? await bundlePart<T>(path, signal) : null;
    if (bundled && !bundled.reading) return bundled;
    const got = await getJson<T>(path, signal, ahead);
    if (!got.reading) return got;
    const now = Date.now();
    if (now >= until) throw new ApiError(503, "busy");
    await pause(Math.min(readingDelay(attempt, now - start), until - now), signal);
  }
}

// ---- Home's one-round-trip bundle (/api/home/bundle) ------------------------------------
// Home asks for seven reads at once; the server composes them from the same kept pages, each
// with its own status. The Home page starts the bundle once (`startHomeBundle`); the reads it
// lists are then answered from it instead of seven requests. The last bundle is kept in this
// tab (sessionStorage, dropped on sign-out and per view-as) and shown at once; when the fresh
// bundle arrives each read is answered again from it (`onHomeBundleFresh`).

const BUNDLE_PARTS: Record<string, string> = {
  "/api/home": "home",
  "/api/sidebar": "sidebar",
  "/api/briefing?pages=true": "briefing",
  "/api/late?offset=0&limit=100": "late",
  "/api/suggestions": "suggestions",
  "/api/replies": "replies",
  "/api/approvals": "approvals",
};
interface BundlePart {
  status: number;
  body: unknown;
}
type Bundle = Record<string, BundlePart>;
const BUNDLE_KEY = "plx.home-bundle";
/** How long a kept bundle may be shown before the fresh one arrives. */
export const BUNDLE_KEPT_MS = 30 * 60 * 1000;
const BUNDLE_KEPT_MOST_CHARS = 1_500_000;

let bundleFresh: Promise<Bundle | null> | null = null;
let bundleKept: Bundle | null = null;
// per part: whether the kept or the fresh answer was last handed out ("fresh": later asks read as usual)
const bundleGiven = new Map<string, "kept" | "fresh">();
const bundleListeners = new Set<() => void>();
onSessionReset(() => {
  bundleFresh = null;
  bundleKept = null;
  bundleGiven.clear();
  if (typeof window !== "undefined") clearKeptBundles();
});

function bundleStorageKey(): string {
  return `${BUNDLE_KEY} ${viewingAs()?.key ?? ""}`;
}

// Whose bundle is kept: the signed-in principal (`me.principal`, set by the auth layer once it is
// known). A kept bundle names the principal and the view-as target it was read for and is never
// shown, nor written, for any other; with no principal known nothing is shown or kept.
let bundlePrincipal: string | null = null;
function clearKeptBundles(except?: { principal: string }): void {
  try {
    for (let i = window.sessionStorage.length - 1; i >= 0; i--) {
      const k = window.sessionStorage.key(i);
      if (!k?.startsWith(BUNDLE_KEY)) continue;
      let keep = false;
      if (except) {
        try {
          keep = (JSON.parse(window.sessionStorage.getItem(k) ?? "null") as { principal?: unknown } | null)?.principal === except.principal;
        } catch {
          keep = false;
        }
      }
      if (!keep) window.sessionStorage.removeItem(k);
    }
  } catch {
    // no storage: nothing was kept
  }
}
/** The auth layer says who is signed in (null: nobody / unknown). Anything kept for another
 * person, or for nobody, is dropped at once. */
export function setBundlePrincipal(principal: string | null): void {
  const was = bundlePrincipal;
  bundlePrincipal = principal;
  // first known principal (null -> P): the bundle asked before it was known was the session's own
  // and stays; every other change drops it
  if (was !== principal && !(was === null && principal !== null)) {
    bundleFresh = null;
    bundleKept = null;
    bundleGiven.clear();
  }
  if (typeof window !== "undefined") clearKeptBundles(principal ? { principal } : undefined);
}

function readKeptBundle(now = Date.now()): Bundle | null {
  try {
    if (!bundlePrincipal) return null;
    const kept = JSON.parse(window.sessionStorage.getItem(bundleStorageKey()) ?? "null") as { at?: unknown; principal?: unknown; as?: unknown; parts?: Bundle } | null;
    if (!kept || kept.principal !== bundlePrincipal || kept.as !== (viewingAs()?.key ?? "") || typeof kept.at !== "number" || now - kept.at > BUNDLE_KEPT_MS || !kept.parts || typeof kept.parts !== "object") return null;
    return kept.parts;
  } catch {
    return null;
  }
}

function keepBundle(parts: Bundle): void {
  // only what was read: an error or a part still being read is never shown from a kept bundle
  const ok: Bundle = {};
  for (const [name, part] of Object.entries(parts)) {
    if (part?.status === 200 && !(part.body as { reading?: boolean | null } | null)?.reading) ok[name] = part;
  }
  if (!bundlePrincipal) return;
  try {
    const text = JSON.stringify({ at: Date.now(), principal: bundlePrincipal, as: viewingAs()?.key ?? "", parts: ok });
    if (text.length <= BUNDLE_KEPT_MOST_CHARS) window.sessionStorage.setItem(bundleStorageKey(), text);
    else window.sessionStorage.removeItem(bundleStorageKey());
  } catch {
    // no storage (a private window, a full quota): the bundle is just not kept
  }
}

/** Read Home's seven first reads in one request (once per page load; a second call does
 * nothing). Call before the reads are asked for. A failed bundle changes nothing: each read
 * is then made on its own. */
export function startHomeBundle(): void {
  if (bundleFresh || typeof window === "undefined") return;
  bundleKept = readKeptBundle();
  bundleGiven.clear();
  // who and as whom this request was made: its answer is stored or shown only while that is still so
  const askedBy = bundlePrincipal;
  const askedAs = viewingAs()?.key ?? "";
  bundleFresh = getJson<{ parts?: Bundle }>("/api/home/bundle").then(
    (got) => {
      const parts = got?.parts && typeof got.parts === "object" ? got.parts : null;
      if ((askedBy !== null && askedBy !== bundlePrincipal) || askedAs !== (viewingAs()?.key ?? "")) return null;
      if (parts) {
        keepBundle(parts);
        // reads answered from the kept bundle are answered again from this one
        if ([...bundleGiven.values()].includes("kept")) for (const f of [...bundleListeners]) f();
      }
      return parts;
    },
    () => null,
  );
}

/** Calls `f` once the fresh bundle arrives after a read was answered from the kept one;
 * returns the unsubscribe. */
export function onHomeBundleFresh(f: () => void): () => void {
  bundleListeners.add(f);
  return () => void bundleListeners.delete(f);
}

/** Whether the bundle may still answer `path` (a bundle is under way and has not already given this read its fresh part). Checked
 * without waiting, so a read the bundle will not answer is sent in the caller's own turn. */
function bundleMayAnswer(path: string): boolean {
  const name = BUNDLE_PARTS[path];
  return !!name && !!bundleFresh && bundleGiven.get(name) !== "fresh";
}

/** The bundle's part for `path` as that read's own answer (a refused part throws what its own
 * request would: same status and code), or null when the read is made on its own. */
async function bundlePart<T>(path: string, signal: AbortSignal): Promise<T | null> {
  const name = BUNDLE_PARTS[path];
  const fresh = bundleFresh;
  if (!name || !fresh) return null;
  const given = bundleGiven.get(name);
  if (given === "fresh") return null;
  let part: BundlePart | undefined;
  if (!given && bundleKept?.[name]) {
    part = bundleKept[name];
    bundleGiven.set(name, "kept");
  } else {
    const parts = await new Promise<Bundle | null>((resolve, reject) => {
      if (signal.aborted) return reject(new DOMException("Aborted", "AbortError"));
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      fresh.then(resolve, reject);
    });
    if (bundleFresh !== fresh) return null;
    part = parts?.[name];
    if (!part) return null;
    bundleGiven.set(name, "fresh");
  }
  if (part.status === 200) return part.body as T;
  const err = (part.body as { error?: { code?: unknown; message?: unknown } } | null)?.error;
  const message = typeof err?.message === "string" ? err.message : undefined;
  throw new ApiError(part.status, typeof err?.code === "string" ? err.code : `http_${part.status}`, incidentOf(message), message?.slice(0, 600));
}

/** An answer the page's head script read before the app loaded (EARLY_READS_SCRIPT). */
interface EarlyAnswer {
  ok: boolean;
  status: number;
  body?: unknown;
}

/** How long after the head script read it an early answer may stand in for a read. */
export const EARLY_MOST_MS = 10_000;

/** The head script's read of `name`, once (a second ask reads again), while it is young; null
 * when there is none. A failed or refused early read is never the answer: the caller reads
 * again through `call` (its own errors, timeout and sign-in handling). */
function takeEarly(name: string, signal?: AbortSignal): Promise<EarlyAnswer> | null {
  if (typeof window === "undefined") return null;
  const early = (window as unknown as { __plxEarly?: { at?: number } & Record<string, Promise<EarlyAnswer> | number | undefined> }).__plxEarly;
  const read = early?.[name] as Promise<EarlyAnswer> | undefined;
  if (!early || !read || typeof read.then !== "function") return null;
  delete early[name];
  if (typeof early.at !== "number" || Date.now() - early.at > EARLY_MOST_MS) return null;
  if (!signal) return read;
  // the caller's cancel ends its wait (the read itself is already sent)
  return new Promise<EarlyAnswer>((resolve, reject) => {
    const abort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal.aborted) return abort();
    signal.addEventListener("abort", abort, { once: true });
    read.then((a) => {
      signal.removeEventListener("abort", abort);
      resolve(a);
    }, reject);
  });
}
onSessionReset(() => {
  if (typeof window !== "undefined") delete (window as unknown as { __plxEarly?: unknown }).__plxEarly;
});

export async function getAuthConfig(signal?: AbortSignal): Promise<AuthConfig> {
  const early = await takeEarly("config", signal);
  if (early?.ok && early.body && typeof early.body === "object") return early.body as AuthConfig;
  return getJson<AuthConfig>("/api/auth/config", signal);
}

export async function getMe(signal?: AbortSignal): Promise<Me> {
  // the head script read it with the session cookie alone: the answer only while no
  // Microsoft token is sent (Entra mode reads it again with the token)
  const early = tokenProvider ? (takeEarly("me"), null) : await takeEarly("me", signal);
  if (early?.ok && early.body && typeof early.body === "object") return early.body as Me;
  return getShared<Me>("/api/me", signal);
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
  /** Already owned (deploy/ops/known-alarms.json): who has it, why, and the date it opens again if not closed. */
  known?: { owner: string; why: string; until: string; needs_action?: boolean };
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
  /** Open alarms that a named owner already has, with the reason and a date (not counted as needing attention). */
  known?: OpsIncident[];
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
export function getOpsAlarms(signal?: AbortSignal, ahead = false): Promise<OpsAlarms> {
  return shared(`json ${readKey("/api/ops/status")}`, (s) => getJson<OpsAlarms>("/api/ops/status", s, ahead), signal);
}

/** Whether a reliability problem is open, from the small read (`?summary=1`: `{configured, open: n}`
 * from the same source as the full list); the full list's `open` array answers the same. Any
 * other shape is an error, never "no problem". Sent as a read-ahead (the nav dot, not content). */
export async function getOpsProblemOpen(signal?: AbortSignal): Promise<boolean> {
  const a = await shared(`json ${readKey("/api/ops/status?summary=1")}`, (s) => getJson<{ configured?: boolean; open?: unknown }>("/api/ops/status?summary=1", s, true), signal);
  const open = a?.open;
  if (typeof open === "number") return !!a.configured && open > 0;
  if (Array.isArray(open)) return !!a.configured && open.length > 0;
  throw new ApiError(502, "ops_summary_shape");
}

export function getHome(signal?: AbortSignal): Promise<HomeResponse> {
  return getRead<HomeResponse>("/api/home", signal);
}

/** Home's unified Needs-you list: every source merged and ranked server-side, never cut. */
export function getHomeNeeds(signal?: AbortSignal): Promise<HomeNeeds> {
  return getRead<HomeNeeds>("/api/home/needs", signal);
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
export function getWatchlist(signal?: AbortSignal, ahead = false): Promise<Watchlist> {
  return getJson<Watchlist>("/api/watch", signal, ahead);
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
/** Every data-health check: the findings you may see and how many are on records you may not open. */
export function getDataHealth(signal?: AbortSignal): Promise<DataHealth> {
  return getJson<DataHealth>("/api/data-health", signal);
}

/** One check's findings from `offset` (the page after those listed), as this reader may see them. */
export function getDataHealthPage(check: string, offset: number, signal?: AbortSignal, limit = 50): Promise<DataHealth> {
  return getJson<DataHealth>(`/api/data-health?check=${encodeURIComponent(check)}&offset=${Math.max(0, Math.floor(offset))}&limit=${limit}`, signal);
}

/** Every list Records offers (one per business view the reader's door serves). */
export function getRecordViews(signal?: AbortSignal): Promise<RecordViews> {
  return getJson<RecordViews>("/api/records/views", signal);
}

type RecordListRead = { filters?: Record<string, string>; q?: string; sort?: string; direction?: string; offset?: number; facets?: boolean };

/** The exact path of one Records list read. Filter keys are sorted so the same logical
 * filters always make the same path (the read store keys by this string). */
export function recordListPath(view: string, f: RecordListRead): string {
  const q = new URLSearchParams({ view, q: f.q ?? "", offset: String(Math.max(0, Math.floor(f.offset ?? 0))), facets: String(f.facets ?? true) });
  if (f.sort) q.set("sort", f.sort);
  if (f.direction) q.set("direction", f.direction);
  const filters = Object.entries(f.filters ?? {}).filter(([, v]) => v).sort(([a], [b]) => a.localeCompare(b));
  for (const [k, v] of filters) q.append("f", `${k}:${v}`);
  return `/api/records/list?${q.toString()}`;
}

/** One Records list: its filters, text, sort and page. `facets` asks for the filters' choices (the first read of a list). */
export function getRecordList(
  view: string,
  f: RecordListRead,
  signal?: AbortSignal,
): Promise<RecordList> {
  return getJson<RecordList>(recordListPath(view, f), signal);
}

/** Where the findings download as a spreadsheet (all checks, or one). */
export function dataHealthExportHref(check?: string): string {
  return check ? `/api/data-health/export?check=${encodeURIComponent(check)}` : "/api/data-health/export";
}

/** Whether the background jobs behind Home's lists run (reply drafts, autopilot entries, late orders): a stopped job is said, never an empty list. */
export function getFeeds(signal?: AbortSignal): Promise<FeedStates> {
  return getJson<FeedStates>("/api/feeds", signal);
}

export function getLateList(offset = 0, signal?: AbortSignal, limit = 25, stale = false): Promise<LateList> {
  const n = Math.min(100, Math.max(1, Math.floor(limit)));
  return getRead<LateList>(`/api/late?offset=${Math.max(0, Math.floor(offset))}&limit=${n}${stale ? "&stale=true" : ""}`, signal);
}

/** Which purchase order: the handle a late list row carries (its first source), or the page's object id. */
export type PoKey = { handle?: string | null; object_id?: string | null };

/** Whether a person closed this order in the Workbench (who, when, why, what came after). */
export async function getPoClosure(key: PoKey, signal?: AbortSignal): Promise<PoClosure> {
  const q = key.handle ? `handle=${encodeURIComponent(key.handle)}` : `object_id=${encodeURIComponent(key.object_id ?? "")}`;
  return (await (await call(`/api/purchase-orders/closure?${q}`, { signal })).json()) as PoClosure;
}

/** Close the order for everyone, in the Workbench only (nothing is written to the source system). */
export async function closePo(key: PoKey, reason: string | null): Promise<PoClosure> {
  return (await (await call("/api/purchase-orders/close", { method: "POST", json: { ...key, reason } })).json()) as PoClosure;
}

/** Undo a close. */
export async function reopenPo(key: PoKey): Promise<PoClosure> {
  return (await (await call("/api/purchase-orders/reopen", { method: "POST", json: { ...key } })).json()) as PoClosure;
}

/** What needs you and what to do: ranked suggestions, each with how we know, a draft and the actions it offers. */
export function getSuggestions(signal?: AbortSignal, ahead = false): Promise<Suggestions> {
  return getRead<Suggestions>("/api/suggestions", signal, ahead);
}

/** One card action as it would be done: the exact payload and request, its checks; nothing is done. */
export async function previewAction(req: ActionRequest, signal?: AbortSignal): Promise<ActionPreview> {
  return (await (await call("/api/actions/preview", { method: "POST", json: req, signal })).json()) as ActionPreview;
}

/** Do the previewed action as yourself, once per idempotency key (a repeat returns the first result). */
export async function confirmAction(req: ActionConfirm): Promise<ActionResult> {
  return (await (await call("/api/actions/confirm", { method: "POST", json: req })).json()) as ActionResult;
}

/** Draft an email for a brief or answer (with its chart attached) for confirm-before-send. */
export async function prepareEmailDraft(req: EmailDraftRequest): Promise<EmailDraftPrepared> {
  return (await (await call("/api/email-draft", { method: "POST", json: req })).json()) as EmailDraftPrepared;
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
  // the card states Home, its late list and its suggestion cards all ask for: one read, kept
  // PANE_REUSE_MS (any write drops it), never one request per component
  return getPaneRead<ActionStates>("/api/actions/state", signal);
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
export function getLabour(signal?: AbortSignal, everyWeek = false): Promise<LabourPage> {
  // the last 12 weeks by default (`older` says more are held); every week on request
  return getJson<LabourPage>(everyWeek ? "/api/labour?weeks=0" : "/api/labour", signal);
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

/** The autopilot area: prepared items you may see, per workflow scores, state and switches. */
export function getAutopilot(signal?: AbortSignal): Promise<AutopilotArea> {
  const key = readKey("/api/autopilot");
  const last = autopilotLast;
  if (last && last.key === key && Date.now() - last.at < AUTOPILOT_REUSE_MS) return Promise.resolve(last.area);
  const before = writesSent;
  const read = getJson<AutopilotArea>("/api/autopilot", signal);
  // an answer read across a write is not kept
  read.then((area) => void (writesSent === before && (autopilotLast = { key, at: Date.now(), area })), () => undefined);
  return read;
}

// Home asks for the autopilot area twice within a second or two (the needs-me panel, then the
// prepared list once your work is in; each 1-2 s on a busy server): a second ask this soon
// gets the first answer. Any write (and a sign-out) drops it, so a decision is never stale.
const AUTOPILOT_REUSE_MS = 10_000;
let autopilotLast: { key: string; at: number; area: AutopilotArea } | null = null;
let writesSent = 0;
onSessionReset(() => (autopilotLast = null));

/** One thing prepared for you: who wrote, what they wrote, and the reply or the question. */
export type PreparedItem = {
  item_id: string; sender: string; channel: "mail" | "teams" | "system"; subject: string; message: string;
  kind: "reply" | "your_call" | "job" | "todo" | "alert"; to: string[]; cc: string[]; body: string; edited: boolean;
  /** On an alert: the alert kind whose items it is the same hold as (Home lists both in one section). */
  alert_kind?: string | null;
  /** On an alert: where it is decided or answered (never from the card), and its records. */
  note?: string; links?: { label: string; href: string }[];
  handed_to?: string; handed_from?: string;
  /** On an Assist matter: open or unverified (its source could not be re-checked), when it was last re-checked, and the draft's state (deferred: draft on request; failed: ask again). */
  state?: "open" | "unverified" | null; verified_at?: string | null; plan_status?: string | null; plan_reason?: string | null;
  plan_at?: string | null; requested_at?: string | null;
  /** On an item nobody is named for: the role whose list it is on until one of them takes it. */
  pooled?: string;
  auto_would?: string;
  /** On a job or a to-do: what the event asks to be done, and each system change proposed for it. */
  summary?: string; jobs?: ProposedJob[];
  /** On a reply the model wrote: it can be written again from one line (replyFromLine). */
  redo?: boolean;
  /** Things to do by hand that no prepared change reaches, on any kind of item. */
  todos?: TodoStep[];
  /** The switch it falls under, and whether your replay score earned Auto for it ("always do this"). */
  category?: string; category_label?: string; always_ok?: boolean;
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

/** A whole job handed to Assist (``/api/assist/jobs``): its plan, progress and prepared pieces. Nothing is
 * sent or written until a change is confirmed on its own card. */
export type AssistPiece = {
  n: number; title: string; instruction: string; status: "prepared" | "nothing" | "failed" | "screened" | "stopped";
  why: string; message: string; summary: string; reply: string;
  ask: { question?: string; options?: { label: string; reply: string }[] } | null;
  proposals: { tool: string; arguments: { system?: string; operation?: string; reason?: string; target?: Record<string, unknown>; change?: Record<string, unknown> } }[];
  todos: { system?: string; record?: string; what?: string }[];
  note: string; warning: string; nothing: string; held_back: { to?: string[]; why?: string }[];
  reads: string[]; tool_reads: { tool?: string; arguments?: unknown; failed?: boolean }[]; reads_traced: boolean;
  records_unread: string[]; unread_reads: { why?: string }[]; planned_records: string[];
  decision: "" | "approved" | "dismissed" | "edited"; edited_text: string; prepared_ids: Record<string, string>;
  changes_over_limit?: number; usd?: string | null;
};
export type AssistJob = {
  job_id: string; text: string; status: "queued" | "running" | "stopping" | "stopped" | "finished" | "failed";
  reason: string; message: string; progress: string; requested_at: string; summary: string;
  checklist: { text: string; piece?: number | null }[]; found: string; not_found: string; nothing: string; planned: number;
  unread: string[]; fallbacks: string[]; plan_reads: string[]; unread_reads: { why?: string }[]; pieces: AssistPiece[]; usd?: number | null;
};
export type AssistJobs = { jobs: AssistJob[]; status: "ok" | "not_run"; reason: string };

export function getAssistJobs(signal?: AbortSignal): Promise<AssistJobs> {
  return getJson<AssistJobs>("/api/assist/jobs", signal);
}
export async function requestAssistJob(text: string): Promise<AssistJob> {
  return (await (await call("/api/assist/jobs", { method: "POST", json: { text } })).json()) as AssistJob;
}
export async function stopAssistJob(jobId: string): Promise<AssistJob> {
  return (await (await call(`/api/assist/jobs/${encodeURIComponent(jobId)}/stop`, { method: "POST" })).json()) as AssistJob;
}
export async function decideAssistPiece(jobId: string, n: number, body: { decision: "approved" | "dismissed" | "edited"; text?: string; edits?: Record<string, Record<string, string>> }): Promise<AssistJob> {
  return (await (await call(`/api/assist/jobs/${encodeURIComponent(jobId)}/pieces/${n}/decision`, { method: "POST", json: body })).json()) as AssistJob;
}
/** Review one proposed change of a piece: prepared as you (read from its system); confirm it on the card. */
export async function prepareAssistChange(jobId: string, n: number, index: number): Promise<PreparedWrite> {
  return (await (await call(`/api/assist/jobs/${encodeURIComponent(jobId)}/pieces/${n}/changes/${index}/prepare`, { method: "POST" })).json()) as PreparedWrite;
}

/** A person's own repeatable Assist workflows (``/api/assist/workflows``): schedule, event or on demand. */
export type AssistWorkflowTriggerKind = "schedule" | "event" | "manual";
export type AssistWorkflowRun = {
  job_id: string; triggered_at: string; kind: "fired" | "run_now"; trigger_note: string; job?: AssistJob | null;
};
export type AssistWorkflow = {
  workflow_id: string; name: string; instruction: string; trigger_kind: AssistWorkflowTriggerKind;
  trigger: Record<string, unknown>; trigger_summary: string; source_kind: "plain_language" | "routine" | "template";
  source_ref: string; status: "active" | "paused" | "stopped"; pause_reason: string;
  created_at: string; updated_at: string; next_run_at?: string | null; last_fired_at?: string | null;
  consecutive_failures: number; backoff_until?: string | null; last_error: string; runs: AssistWorkflowRun[];
};
export type AssistWorkflowTemplate = {
  template_id: string; name: string; description: string; trigger_kind: AssistWorkflowTriggerKind;
  trigger: Record<string, unknown>; trigger_summary: string; instruction: string;
};
export type AssistMinedRoutine = {
  routine_id: string; kind: string; description: string; trigger: Record<string, unknown>;
  mined: Record<string, unknown>; validated: Record<string, unknown>; status: string;
  suggested_name: string; suggested_trigger_summary: string;
};
export type AssistWorkflows = {
  workflows: AssistWorkflow[]; routines: AssistMinedRoutine[]; templates: AssistWorkflowTemplate[];
  status: "ok" | "not_run"; reason: string;
};
export type AssistWorkflowParsed = {
  name: string; instruction: string; trigger_kind: AssistWorkflowTriggerKind;
  trigger: Record<string, unknown>; trigger_summary: string;
};
export type AssistWorkflowCreate = {
  plain_text?: string; template_id?: string; routine_id?: string; name?: string; instruction?: string;
  trigger_kind?: AssistWorkflowTriggerKind; trigger?: Record<string, unknown>;
};
export type AssistWorkflowRunResult = { workflow: AssistWorkflow; job: AssistJob };

export function getAssistWorkflows(signal?: AbortSignal): Promise<AssistWorkflows> {
  return getJson<AssistWorkflows>("/api/assist/workflows", signal);
}
export async function parseAssistWorkflow(text: string): Promise<AssistWorkflowParsed> {
  return (await (await call("/api/assist/workflows/parse", { method: "POST", json: { text } })).json()) as AssistWorkflowParsed;
}
export async function createAssistWorkflow(body: AssistWorkflowCreate): Promise<AssistWorkflow> {
  return (await (await call("/api/assist/workflows", { method: "POST", json: body })).json()) as AssistWorkflow;
}
export async function updateAssistWorkflow(id: string, body: { name?: string; instruction?: string; trigger_kind?: AssistWorkflowTriggerKind; trigger?: Record<string, unknown> }): Promise<AssistWorkflow> {
  return (await (await call(`/api/assist/workflows/${encodeURIComponent(id)}`, { method: "PATCH", json: body })).json()) as AssistWorkflow;
}
export async function pauseAssistWorkflow(id: string): Promise<AssistWorkflow> {
  return (await (await call(`/api/assist/workflows/${encodeURIComponent(id)}/pause`, { method: "POST" })).json()) as AssistWorkflow;
}
export async function resumeAssistWorkflow(id: string): Promise<AssistWorkflow> {
  return (await (await call(`/api/assist/workflows/${encodeURIComponent(id)}/resume`, { method: "POST" })).json()) as AssistWorkflow;
}
export async function stopAssistWorkflow(id: string): Promise<AssistWorkflow> {
  return (await (await call(`/api/assist/workflows/${encodeURIComponent(id)}/stop`, { method: "POST" })).json()) as AssistWorkflow;
}
export async function runAssistWorkflow(id: string): Promise<AssistWorkflowRunResult> {
  return (await (await call(`/api/assist/workflows/${encodeURIComponent(id)}/run`, { method: "POST" })).json()) as AssistWorkflowRunResult;
}

/** Your own Teams sign-in, so a Teams reply you confirm posts as you (never the token). */
export type TeamsConnection = { connected: boolean; scopes?: string[]; connected_at?: string | null; used_at?: string | null; missing?: string[] };

export function getTeamsConnection(signal?: AbortSignal): Promise<TeamsConnection> {
  return getPaneRead<TeamsConnection>("/api/teams/connection", signal);   // shared with the page's early read; a write drops it
}

/** Start your one-time Teams sign-in: the Microsoft address to open. */
export async function startTeamsConnect(): Promise<{ url: string }> {
  return (await (await call("/api/teams/connect", { method: "POST" })).json()) as { url: string };
}

export async function forgetTeams(): Promise<TeamsConnection> {
  return (await (await call("/api/teams/connection", { method: "DELETE" })).json()) as TeamsConnection;
}

/** The list: what is shown, how many more are older than its 14 days, and whether it is off or stale (said, never hidden). */
export type DraftList = { items: PreparedItem[]; never_senders?: string[]; older?: number; status?: string | null; reason?: string; stale_since?: string | null };

/** Your open work, newest first (the one list Home reads too); the last 14 days unless ``allAges``. */
export async function getAutopilotDrafts(signal?: AbortSignal, allAges = false, ahead = false): Promise<DraftList> {
  return getJson<DraftList>(allAges ? "/api/autopilot/drafts?all_ages=true" : "/api/autopilot/drafts", signal, ahead);
}

/** Ask for this item's draft now; it is prepared and the list shows it when ready. */
export async function requestDraft(itemId: string): Promise<{ status: "working" }> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/draft`, { method: "POST" })).json()) as { status: "working" };
}

/** Never (or again) prepare anything for you from one sender; your list comes back. */
export async function setNeverSender(sender: string, never: boolean): Promise<{ items: PreparedItem[]; never_senders?: string[] }> {
  return (await (await call("/api/autopilot/never", { method: "POST", json: { sender, never } })).json()) as { items: PreparedItem[]; never_senders?: string[] };
}

/** Whom an alert on your list can be passed to: a key and a name each. */
export function getAutopilotColleagues(signal?: AbortSignal): Promise<ViewAsPeople> {
  return getJson<ViewAsPeople>("/api/autopilot/colleagues", signal);
}

/** Pass an alert on your list to a colleague: on their list too, only to set aside there. */
export async function handAutopilotItem(itemId: string, to: string): Promise<{ items: PreparedItem[] }> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/hand`, { method: "POST", json: { to } })).json()) as { items: PreparedItem[] };
}

/** Take an item your role holds (nobody named for it): yours alone from now. */
export async function takeAutopilotItem(itemId: string): Promise<{ items: PreparedItem[] }> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/take`, { method: "POST" })).json()) as { items: PreparedItem[] };
}

/** Always do this: Auto for this item's sender, or its whole switch (only where it was earned). */
export async function alwaysDoThis(itemId: string, scope: "sender" | "category"): Promise<AutopilotArea> {
  return (await (await call(`/api/autopilot/items/${encodeURIComponent(itemId)}/always`, { method: "POST", json: { scope } })).json()) as AutopilotArea;
}

/** Your own limits on one switch's Auto: senders, days, hours (US Eastern), a stricter bar. */
export type SwitchLimits = { senders: string[]; days: number[]; start?: string | null; end?: string | null; min_percent?: number | null };
export async function setSwitchLimits(choice: string, limits: SwitchLimits): Promise<AutopilotArea> {
  return (await (await call("/api/autopilot/conditions", { method: "POST", json: { choice, ...limits } })).json()) as AutopilotArea;
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

/** Set one category of your own work to off, confirm (prepared in full; one click of yours
 * performs it) or auto (performed by itself only where your replay score earned it; 409
 * otherwise). Answers the area as it now reads. */
export async function setMyWorkMode(choice: string, mode: "off" | "confirm" | "auto"): Promise<AutopilotArea> {
  return (await (await call("/api/autopilot/mode", { method: "POST", json: { choice, mode } })).json()) as AutopilotArea;
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
export function bizPage(kind: BizKind, key: { h?: string; chain?: string; record?: string }, signal?: AbortSignal, reuse = false): Promise<BizPage> {
  const q = new URLSearchParams({ kind });
  // identifiers whole: a cut handle is another handle (the server refuses one past its bound)
  if (key.h) q.set("h", key.h);
  if (key.chain) q.set("chain", key.chain);
  // a record id: the page of the row it was read into (404 when the door has none)
  if (key.record && !key.h) q.set("record", key.record);
  const path = `/api/biz/page?${q.toString()}`;
  return reuse ? getPaneRead<BizPage>(path, signal) : getJson<BizPage>(path, signal);
}

/** Projection type names (PurchaseOrder, SalesOrder, ...). Anything else fails closed. */
const OBJECT_TYPE = /^[A-Za-z][A-Za-z0-9_]{0,59}$/;

export function isObjectType(t: string): boolean {
  return OBJECT_TYPE.test(t);
}

// A record page read when its link is hovered (`prefetchRecord`), taken once by the page's own
// read if it asks within AHEAD_RECORD_MS, so a click lands on a read already under way or done.
// Kept for the person who hovered only (key: view-as), dropped on any write and a sign-out; the
// server decides what each read returns as it does for any other read.
const AHEAD_RECORD_MS = 5000;
const recordAhead = new Map<string, { at: number; read: Promise<unknown> }>();
onSessionReset(() => recordAhead.clear());
function takeAhead<T>(path: string): Promise<T> | null {
  const k = readKey(path);
  const hit = recordAhead.get(k);
  if (!hit) return null;
  recordAhead.delete(k);
  return Date.now() - hit.at < AHEAD_RECORD_MS ? (hit.read as Promise<T>) : null;
}

/** Read a record's page ahead (its link is hovered or focused), once; a failure is dropped. */
export function prefetchRecord(type: string, id: string): void {
  if (!isObjectType(type) || !id || id.length > 700) return;
  const base = `/api/object/${type}/${encodeURIComponent(id)}`;
  const reads: [string, () => Promise<unknown>][] = [
    [`${base}?quick=1&core=1`, () => getJson<ObjectPage>(`${base}?quick=1&core=1`)],
    [`${base}?quick=1`, () => getRead<ObjectPage>(`${base}?quick=1`)],
  ];
  for (const [path, run] of reads) {
    const k = readKey(path);
    const have = recordAhead.get(k);
    if (have && Date.now() - have.at < AHEAD_RECORD_MS) continue;
    const entry = { at: Date.now(), read: run() };
    recordAhead.set(k, entry);
    entry.read.catch(() => {
      if (recordAhead.get(k) === entry) recordAhead.delete(k);
    });
  }
}

/** A record's page; `quick`: a first read without the checks (`complete` false) to show
 * while the full page is read. */
export function getObject(type: string, id: string, signal?: AbortSignal, quick = false): Promise<ObjectPage> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  const path = `/api/object/${type}/${encodeURIComponent(id)}${quick ? "?quick=1" : ""}`;
  return (quick && !signal?.aborted ? takeAhead<ObjectPage>(path) : null) ?? getRead<ObjectPage>(path, signal);
}

/** A record's core read: its own details only (`core`, status "reading"), served before the
 * quick read while its status, timeline, links and documents are read. */
export function getObjectCore(type: string, id: string, signal?: AbortSignal): Promise<ObjectPage> {
  if (!isObjectType(type) || !id || id.length > 700) return Promise.reject(new ApiError(404, "not_found"));
  const path = `/api/object/${type}/${encodeURIComponent(id)}?quick=1&core=1`;
  return (!signal?.aborted ? takeAhead<ObjectPage>(path) : null) ?? getJson<ObjectPage>(path, signal);
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
  // a purchase order's tab is read from the served store (one stored read; same shape)
  if (type === "PurchaseOrder") return getJson<RecordHistory>(`/api/page/history/${encodeURIComponent(id)}${qs}`, signal);
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

export function getObjectSection(section: SectionName, type: string, id: string, signal?: AbortSignal, reuse = false): Promise<ObjectSection> {
  if (!PAGE_SECTIONS.includes(section) || !isObjectType(type) || !id || id.length > 700)
    return Promise.reject(new ApiError(404, "not_found"));
  const path = `/api/object-section/${section}/${type}/${encodeURIComponent(id)}`;
  // reuse: a pane's read (Home's detail), kept a few seconds (``getPaneRead``)
  return reuse ? getPaneRead<ObjectSection>(path, signal) : getJson<ObjectSection>(path, signal);
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

/** What Ask and Genie asked for that nothing defines, most asked first (the owner and the admins only). */
export function getGaps(signal?: AbortSignal): Promise<GapList> {
  return getJson<GapList>("/api/answers/gaps", signal);
}

/** Candidate shared measure definitions captured from usage or proposed for review. */
export function getMeasureCandidates(status?: MeasureCandidateStatus, signal?: AbortSignal): Promise<MeasureCandidateList> {
  const tail = status ? `?status=${encodeURIComponent(status)}` : "";
  return getJson<MeasureCandidateList>(`/api/measures/candidates${tail}`, signal);
}

/** Propose a saved chart, accepted answer, or SQL statement as a shared measure definition. */
export async function proposeMeasureCandidate(body: MeasureCandidatePropose): Promise<MeasureCandidate> {
  const res = await call("/api/measures/candidates", { method: "POST", json: body });
  return (await res.json()) as MeasureCandidate;
}

/** Owner-only: approve a candidate and promote it into live shared measure definitions. */
export async function approveMeasureCandidate(id: string, body?: MeasureCandidateApprove): Promise<MeasureCandidate> {
  const res = await call(`/api/measures/candidates/${encodeURIComponent(id)}/approve`, { method: "POST", json: body ?? {} });
  return (await res.json()) as MeasureCandidate;
}

/** Owner-only: dismiss a candidate measure definition from the review queue. */
export async function dismissMeasureCandidate(id: string, body?: MeasureCandidateDismiss): Promise<MeasureCandidate> {
  const res = await call(`/api/measures/candidates/${encodeURIComponent(id)}/dismiss`, { method: "POST", json: body ?? {} });
  return (await res.json()) as MeasureCandidate;
}

/** How the Ask has been doing per day: answers, failures, time and steps (the owner and the admins only). */
export function getObserved(signal?: AbortSignal): Promise<AskObserved> {
  return getJson<AskObserved>("/api/answers/observed", signal);
}

/** Every answer feedback entry of a status, paged through whole (the server pages by 200). */
export async function getAllFeedback(filters: { kind: string; status?: string }, signal?: AbortSignal): Promise<FeedbackList> {
  let out: FeedbackList | null = null;
  for (;;) {
    const page = await getFeedback({ ...filters, offset: out?.entries.length ?? 0, limit: 200 }, signal);
    out = out ? { ...page, entries: [...out.entries, ...page.entries], offset: 0 } : page;
    if (page.entries.length === 0 || out.entries.length >= page.total) return out;
  }
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

/** POST /api/ask/files: the file as the request body (streamed by the browser to the server's
 * disk; any size). Answers the id the question names it by. */
export async function uploadAskFile(file: Blob, name: string, signal?: AbortSignal): Promise<{ id: string; name: string; size: number; digest: string }> {
  viewAsHeader("/api/ask/files", "POST");
  const res = await fetch(`/api/ask/files?name=${encodeURIComponent(name)}`, {
    method: "POST",
    credentials: "same-origin",
    headers: await headers({ "Content-Type": "application/octet-stream" }),
    body: file,
    signal,
  });
  if (!res.ok) throw await apiErrorFrom(res);
  return (await res.json()) as { id: string; name: string; size: number; digest: string };
}

/** DELETE /api/ask/files/<id>: take an uploaded file off. */
export async function removeAskFile(id: string): Promise<void> {
  viewAsHeader("/api/ask/files", "DELETE");
  const res = await fetch(`/api/ask/files/${encodeURIComponent(id)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: await headers({}),
  });
  if (!res.ok && res.status !== 404) throw await apiErrorFrom(res);
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
export function workLogPath(filters: WorkLogFilters = {}): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) if (v !== undefined && v !== "" && v !== false) q.set(k, String(v));
  const tail = q.toString();
  return `/api/work-log${tail ? `?${tail}` : ""}`;
}
export function getWorkLog(filters: WorkLogFilters = {}, signal?: AbortSignal): Promise<WorkLogPage> {
  return getJson<WorkLogPage>(workLogPath(filters), signal);
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

/** Start a warm Ask session when the person opens or focuses the Ask box (throttled per tab). */
const WARM_ASK_COOLDOWN_MS = 30_000;
let lastWarmAskAt = 0;
let warmAskRefused = false;

export function resetWarmAsk(): void {
  lastWarmAskAt = 0;
  warmAskRefused = false;
}
onSessionReset(resetWarmAsk);

export function warmAsk(): void {
  if (warmAskRefused) return;
  const now = Date.now();
  if (now - lastWarmAskAt < WARM_ASK_COOLDOWN_MS) return;
  lastWarmAskAt = now;
  void (async () => {
    try {
      viewAsHeader("/api/ask/warm", "POST");
      const res = await fetch("/api/ask/warm", { method: "POST", credentials: "same-origin", headers: await headers({}) });
      if (res.status === 403 || res.status === 405) warmAskRefused = true;
    } catch { // guard-ok: best-effort session warm-up on focus, never disrupts typing
      return;
    }
  })();
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
  // Home's commitments and the record page's memory both ask: one read, kept PANE_REUSE_MS (a write drops it)
  return getPaneRead<MemoryItems>("/api/memory", signal);
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

/** How many tiles the one-response dashboard sends whole (about the first screen); the rest are
 * read from their stored value as they near the view. */
export const INSIGHTS_EAGER = 8;

/** One line of the one-response dashboard: your list first, then each chart as it was read
 * (its result, or its own error: one chart failing never fails the others; `later`: a measure
 * chart with nothing kept for you, which reads itself once it nears the view). */
export type InsightsLine =
  | { list: InsightList }
  | { id: string; result?: InsightResult; error?: { code: string; message?: string; status: number }; later?: boolean; waiting?: boolean };

/** What the dashboard showed last (per person and view-as): the next visit draws it at once,
 * then reads again and replaces it. Dropped on sign-out, any write and a view-as change. */
const insightsKept = new Map<string, { list: InsightList; tiles: Record<string, InsightResult> }>();
onSessionReset(() => insightsKept.clear());
const insightsKeptKey = () => viewingAs()?.key ?? "";
export function keptInsights(): { list: InsightList; tiles: Record<string, InsightResult> } | undefined {
  return insightsKept.get(insightsKeptKey());
}
export function keepInsights(list: InsightList, tiles: Record<string, InsightResult>): void {
  insightsKept.set(insightsKeptKey(), { list, tiles });
}

/** The whole dashboard in ONE request: `on` gets each line as it arrives (the list, then every
 * chart as it was read). Each chart was checked for your access on its own, server side.
 * The request belongs to the dashboard, not to the page showing it: leaving the page (or the
 * page reading again) only stops listening, the stream is never cancelled; a page opened while
 * it runs gets the lines already read, then the rest. It stops only when the session ends. */
interface InsightsRun {
  as: string;
  lines: InsightsLine[];
  listeners: Set<(line: InsightsLine) => void>;
  result: Promise<void>;
  ctl: AbortController;
  over: boolean;
}
let insightsRun: InsightsRun | null = null;
onSessionReset(() => {
  insightsRun?.ctl.abort();
  insightsRun = null;
});

async function readInsightsStream(run: InsightsRun): Promise<void> {
  // the first INSIGHTS_EAGER tiles come with their stored value; every other tile reads its own
  // when it nears the view (the page was 484 KB for 40 charts drawn at once)
  const res = await call(`/api/insights/all/stream?eager=${INSIGHTS_EAGER}`, { signal: run.ctl.signal });
  if (!res.body) throw new ApiError(502, "invalid_response");
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  const feed = (text: string) => {
    buf += text;
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      const got = JSON.parse(line) as InsightsLine;
      run.lines.push(got);
      for (const f of [...run.listeners]) f(got);
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    feed(dec.decode(value, { stream: true }));
  }
  feed(dec.decode() + "\n");
}

export function streamInsights(on: (line: InsightsLine) => void, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(signal.reason ?? new DOMException("aborted", "AbortError"));
  let run = insightsRun;
  if (!run || run.over || run.as !== (viewingAs()?.key ?? "")) {
    const made: InsightsRun = { as: viewingAs()?.key ?? "", lines: [], listeners: new Set(), result: Promise.resolve(), ctl: new AbortController(), over: false };
    made.result = readInsightsStream(made).finally(() => {
      made.over = true;
      if (insightsRun === made) insightsRun = null;
    });
    made.result.catch(() => undefined);
    insightsRun = made;
    run = made;
  }
  const r = run;
  for (const line of [...r.lines]) on(line);
  r.listeners.add(on);
  return new Promise<void>((resolve, reject) => {
    const stop = () => {
      r.listeners.delete(on);
      reject(signal?.reason ?? new DOMException("aborted", "AbortError"));
    };
    signal?.addEventListener("abort", stop, { once: true });
    r.result.then(
      () => {
        signal?.removeEventListener("abort", stop);
        r.listeners.delete(on);
        resolve();
      },
      (e) => {
        signal?.removeEventListener("abort", stop);
        r.listeners.delete(on);
        reject(e);
      },
    );
  });
}

/** One step of "why did this change": what was read, its rows, and the best answer so far (stopping
 * after any step leaves a true answer). A step that could not be read has `error` and ends it. */
export type WhyRank = { dimension: string | null; parts: string | null; parts_for_half: string | null; top_part: string | null; top_change: string | null; top_share_of_change_pct: string | null; whole_change: string | null; unknown_parts: string | null; concentration_pct: string | null; [unit: string]: string | null };
export type WhyAsk = { drill?: string; period?: string; against?: string; filter?: Record<string, string | string[]> };
export type WhyStep = { step: number; of: number; says: string; period?: string; columns?: string[]; rows?: (string | null)[][]; ranking?: WhyRank[]; answer: string; error?: { code: string; message?: string; status: number } };

/** Stream the steps of a measure tile's "why did this change"; abort `signal` to stop early (Answer now). */
export async function streamWhy(id: string, on: (step: WhyStep) => void, signal?: AbortSignal, ask: WhyAsk = {}): Promise<void> {
  const q = new URLSearchParams();
  if (ask.drill) q.set("drill", ask.drill);
  if (ask.period) q.set("period", ask.period);
  if (ask.against) q.set("against", ask.against);
  if (ask.filter) q.set("filter", JSON.stringify(ask.filter));
  const res = await call(`/api/insights/${encodeURIComponent(id)}/why${q.size ? `?${q}` : ""}`, { signal });
  if (!res.body) throw new ApiError(502, "invalid_response");
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  const feed = (text: string) => {
    buf += text;
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) on(JSON.parse(line) as WhyStep);
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    feed(dec.decode(value, { stream: true }));
  }
  feed(dec.decode() + "\n");
}

/** One insight as you read it (kept from your read of the last few minutes; `again`: read
 * now): its statement, rows, chart and the people they name. */
export async function getInsight(id: string, signal?: AbortSignal, again = false, onStored?: (old: InsightResult) => void): Promise<InsightResult> {
  // the server never reads a tile in this request: a stored value comes back at once with its
  // read time; one with nothing stored yet says so (425, `insight_filling`) while its read runs
  // in the background, and is asked for again until it is stored (no deadline: it is not cut).
  // A Refresh gets the stored value at once (`onStored`, so it stays drawn) and is asked for
  // again until a newer one is stored. Polling waits while the page is hidden.
  let refreshing = again;
  let old: string | undefined;
  for (;;) {
    try {
      const got = await getJson<InsightResult>(`/api/insights/${encodeURIComponent(id)}${refreshing ? "?again=true" : ""}`, signal);
      if (!again || (old !== undefined && got.read_at !== old)) return got;
      if (old === undefined) { old = got.read_at; onStored?.(got); }
      refreshing = false;
    } catch (e) {
      if (!(e instanceof ApiError && e.code === "insight_filling") || signal?.aborted) throw e;
    }
    await insightPause(signal);
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
  }
}

/** Wait one poll interval, and while the page is hidden until it is shown again. */
function insightPause(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const done = () => { clearTimeout(timer); document.removeEventListener("visibilitychange", wake); signal?.removeEventListener("abort", done); resolve(); };
    const wake = () => { if (!document.hidden) timer = setTimeout(done, INSIGHT_FILL_POLL_MS); else clearTimeout(timer); };
    signal?.addEventListener("abort", done);
    document.addEventListener("visibilitychange", wake);
    if (document.hidden) return;   // resumes on visibilitychange
    timer = setTimeout(done, INSIGHT_FILL_POLL_MS);
  });
}
const INSIGHT_FILL_POLL_MS = 3000;

/** One insight read now with filters you changed or took out (its own conditions only); nothing kept. */
export async function readInsightFiltered(id: string, edits: FilterEdit[]): Promise<InsightResult> {
  const res = await call(`/api/insights/${encodeURIComponent(id)}/filtered`, { method: "POST", json: { edits } });
  return (await res.json()) as InsightResult;
}

/** An answer's query read now, as you, with filters you changed or took out; nothing saved. */
export async function readStatement(sql: string, edits: FilterEdit[]): Promise<StatementRead> {
  const res = await call("/api/statement/read", { method: "POST", json: { sql, edits } });
  return (await res.json()) as StatementRead;
}

/** Keep your own arrangement of the dashboard (the order of your charts, the ones taken off);
 * the dashboard comes back as it now is. */
export async function saveInsightLayout(order: string[], hidden: string[]): Promise<InsightList> {
  const res = await call("/api/insights/layout", { method: "PUT", json: { order, hidden } });
  return (await res.json()) as InsightList;
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

/** Export any chart, table or answer into one formatted Excel (.xlsx) workbook. */
export async function exportXlsx(req: ExportXlsxRequest): Promise<Blob> {
  const res = await call("/api/export/xlsx", { method: "POST", json: req });
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
  const q = (question ?? "").trim();   // whole: the server takes a question up to QUESTION_MAX
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

/** Your recurring instructions ("every Monday ..."); only yours. */
export function getRecurring(signal?: AbortSignal): Promise<RecurringInstructions> {
  return getPaneRead<RecurringInstructions>("/api/instructions/recurring", signal);   // shared with the page's early read; a write drops it
}

/** Keep a recurring instruction: when it runs is read from the line on the server. */
export async function keepRecurring(body: RecurringKeep): Promise<RecurringInstruction> {
  return (await (await call("/api/instructions/recurring", { method: "POST", json: body })).json()) as RecurringInstruction;
}

/** Change one of yours: its hour, weekday or name, or pause and resume it. */
export async function changeRecurring(id: string, body: RecurringChange): Promise<RecurringInstruction> {
  return (await (await call(`/api/instructions/recurring/${encodeURIComponent(id)}`, { method: "PATCH", json: body })).json()) as RecurringInstruction;
}

/** Stop one of yours: it is deleted and never runs again. */
export async function stopRecurring(id: string): Promise<void> {
  await call(`/api/instructions/recurring/${encodeURIComponent(id)}`, { method: "DELETE" });
}
