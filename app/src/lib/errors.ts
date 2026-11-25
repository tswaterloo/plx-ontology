// Plain-language errors. Server text is never shown verbatim: a known code
// maps to a sentence written here; everything else gets a generic one. The
// only server value ever shown is an incident id, and only when it is a short
// hex token.

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly incident?: string,
    /** The server's plain sentence (a refused rule's reason). */
    public readonly detail?: string,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

const BY_CODE: Record<string, string> = {
  // operator_api codes (operator_api/app.py MESSAGES)
  search_busy: "The search is taking too long. Try again in a moment.",
  sign_in_required: "Your session has ended. Sign in again to continue.",
  view_as_refused: "You are viewing as someone else, which is read only and covers Home, alerts and drafts. Stop viewing to do this.",
  unauthenticated: "Your session has ended. Sign in again to continue.",
  no_workbench_access: "You're signed in, but your account doesn't have Workbench access yet. Ask your manager or IT to grant it.",
  insufficient_scope: "Your sign-in doesn't include Workbench access. Sign out and sign in again.",
  dev_ticket_required: "Signing in here needs the access link you were given.",
  dev_ticket_not_accepted: "This demo sign-in doesn't take an access link. Leave it empty.",
  access_link_refused: "This access link has expired or isn't valid. Ask for a new one.",
  dev_login_not_served: "Demo sign-in is off here. Sign in with Microsoft.",
  not_found: "Couldn't find that, or you don't have access to it.",
  alerts_not_read: "Alerts haven't been read on this server yet. This fills in after the next alert run.",
  invalid_request: "That request wasn't understood. Try rephrasing it.",
  origin_refused: "This page isn't trusted by the Workbench. Open the Workbench from its usual link.",
  not_served: "That part of the Workbench is off on this server.",
  busy: "The server is busy. Try again in a minute.",
  action_refused: "That action can't be done as it stands. Review it again to see what needs changing.",
  ask_not_served: "Ask is off on this server. Today and record pages still work.",
  invalid_measure_id: "The measure id must be lowercase letters, numbers, or underscores starting with a letter.",
  measure_id_exists: "A built-in definition with that id already exists. Choose another name.",
  invalid_measure: "That definition is missing a required field or references an unknown column.",
  excluded_content: "This statement names a person or value that cannot be saved as a shared definition.",
  // app-side codes
  canary_route_refused: "This section isn't available in a read-only session.",
  forbidden: "You don't have access to this. Ask the owner or your team lead if you need it.",
  rate_limited: "Too many questions at once. Try again in a few seconds.",
  timeout: "Timed out. Try a narrower question, or try again in a moment.",
  read_timeout: "The Workbench took too long to answer. Try again in a moment.",
  connection_stalled: "The connection stalled: nothing arrived for 2 minutes. Retry.",
  upstream_unavailable: "One of the business systems isn't answering right now. Try again in a few minutes.",
  stream_interrupted: "The answer was cut off. Ask again to get the full answer.",
  network: "Can't reach the Workbench. Check your connection and try again.",
  microsoft_sign_in_failed: "Microsoft sign-in didn't finish. Try again.",
  microsoft_sign_in_misconfigured: "Microsoft sign-in isn't set up correctly on this server. Tell the Workbench team.",
};

/** Insights refusals whose sentence the server writes (operator_api/insights.py REFUSED): each
 * names what is missing (a column the business tables don't have yet, a read past its time limit),
 * so a tile nobody here wrote is never answered with "try rephrasing it". */
export const INSIGHT_CODES: ReadonlySet<string> = new Set([
  "insight_not_built", "insight_gone", "insight_not_ready", "insight_too_slow", "insight_withheld",
  "insight_unreadable", "insight_invalid_sql", "insight_chart", "insight_too_many", "insight_not_saved",
  "insight_watch", "filter_unworkable", "view_as_statement",
]);

/** Errors a second try cannot change (the record is missing, or the reader may not see it). */
export const FINAL_ERRORS: ReadonlySet<string> = new Set([BY_CODE.not_found, BY_CODE.forbidden, BY_CODE.alerts_not_read, BY_CODE.view_as_refused, BY_CODE.canary_route_refused]);
/** Not a failure: a note on what this view covers (viewing as someone), said calmly, never in red. */
export const ACCESS_NOTES: ReadonlySet<string> = new Set([BY_CODE.view_as_refused, BY_CODE.canary_route_refused]);

const BY_STATUS: Record<number, string> = {
  401: BY_CODE.sign_in_required,
  403: BY_CODE.forbidden,
  404: BY_CODE.not_found,
  408: BY_CODE.timeout,
  422: BY_CODE.invalid_request,
  429: BY_CODE.rate_limited,
  502: BY_CODE.upstream_unavailable,
  503: BY_CODE.upstream_unavailable,
  504: BY_CODE.timeout,
};

export const GENERIC_ERROR = "The Workbench server failed on this request. Try again; if it keeps happening, tell the Workbench team.";

const INCIDENT = /\bincident ([0-9a-f]{6,16})\b/;

/** The incident id from an operator_api "internal" message, if it has one. */
export function incidentOf(message: unknown): string | undefined {
  return typeof message === "string" ? message.match(INCIDENT)?.[1] : undefined;
}

function generic(incident?: string): string {
  return incident ? `${GENERIC_ERROR.replace(/\.$/, "")} (incident ${incident}).` : GENERIC_ERROR;
}

/** Server states shown as a plain banner rather than an error. */
export type NoticeKind = "busy" | "off";

export function noticeKind(err: unknown): NoticeKind | undefined {
  const code = err && typeof err === "object" && "code" in err ? (err as { code: unknown }).code : undefined;
  return code === "busy" ? "busy" : code === "ask_not_served" ? "off" : undefined;
}

/** One plain sentence for any thrown value or stream error body. */
export function plainError(err: unknown): string {
  if (err instanceof ApiError) {
    // a check not current says itself when it last ran ("... unavailable since Nov 18, 3:07 PM ET")
    if (err.code === "alerts_stale" && err.detail) return err.detail;
    if (INSIGHT_CODES.has(err.code) && err.detail) return err.detail;
    return BY_CODE[err.code] ?? (err.code === "internal" ? generic(err.incident) : BY_STATUS[err.status]) ?? generic(err.incident);
  }
  if (err instanceof TypeError) return BY_CODE.network; // fetch network failure
  if (err && typeof err === "object" && "code" in err) {
    const { code, message } = err as { code: unknown; message?: unknown };
    if (typeof code === "string" && BY_CODE[code]) return BY_CODE[code];
    if (code === "internal") return generic(incidentOf(message));
  }
  return GENERIC_ERROR;
}

/** One plain sentence for a dashboard tile that could not be read: what the server said was
 * missing, else the plain sentence for its code, never the "rephrase it" one (a tile is a
 * statement the reader did not type). */
export function tileError(err: unknown): string {
  const said = plainError(err);
  return said === BY_CODE.invalid_request ? "This view could not be read just now. Refresh to try again; if it keeps failing, tell the Workbench team." : said;
}

/** Read the error code (and incident) from a failed response; never throws. */
export async function apiErrorFrom(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as { code?: unknown; error?: { code?: unknown; message?: unknown } };
    const code = body?.error?.code ?? body?.code;
    const message = body?.error?.message;
    return new ApiError(res.status, typeof code === "string" ? code : `http_${res.status}`, incidentOf(message), typeof message === "string" ? message.slice(0, 600) : undefined);
  } catch {
    return new ApiError(res.status, `http_${res.status}`);
  }
}
