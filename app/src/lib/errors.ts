// Plain-language errors. Server text is never shown verbatim: a known code
// maps to a sentence written here; everything else gets a generic one. The
// only server value ever shown is an incident id, and only when it is a short
// hex token.

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    public readonly incident?: string,
  ) {
    super(code);
    this.name = "ApiError";
  }
}

const BY_CODE: Record<string, string> = {
  // operator_api codes (operator_api/app.py MESSAGES)
  sign_in_required: "Your session has ended. Sign in again to continue.",
  unauthenticated: "Your session has ended. Sign in again to continue.",
  no_workbench_access: "You're signed in, but your account doesn't have Workbench access yet. Ask your manager or IT to grant it.",
  insufficient_scope: "Your sign-in doesn't include Workbench access. Sign out and sign in again.",
  dev_ticket_required: "This demo sign-in needs the access link you were given.",
  dev_ticket_not_accepted: "This demo sign-in doesn't take an access link. Leave it empty.",
  dev_login_not_served: "Demo sign-in is off here. Sign in with Microsoft.",
  not_found: "We couldn't find that, or you don't have access to it.",
  invalid_request: "That request wasn't understood. Try rephrasing it.",
  origin_refused: "This page isn't trusted by the Workbench. Open the Workbench from its usual link.",
  not_served: "That part of the Workbench isn't switched on here yet.",
  busy: "The Workbench is answering several questions right now. Try again in a minute.",
  ask_not_served: "Asking questions isn't switched on on this server yet. Home and record pages still work.",
  // app-side codes
  forbidden: "You don't have access to this. Ask the owner or your team lead if you need it.",
  rate_limited: "Lots of questions at once. Give it a few seconds and try again.",
  timeout: "That took too long to answer. Try a narrower question, or try again in a moment.",
  upstream_unavailable: "One of the business systems isn't answering right now. Try again in a few minutes.",
  stream_interrupted: "The answer was cut off. Ask again to get the full answer.",
  network: "Can't reach the Workbench. Check your connection and try again.",
  microsoft_sign_in_failed: "Microsoft sign-in didn't finish. Try again.",
  microsoft_sign_in_misconfigured: "Microsoft sign-in isn't set up correctly on this server. Tell the Workbench team.",
};

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

export const GENERIC_ERROR = "Something went wrong on our side. Try again; if it keeps happening, tell the Workbench team.";

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

/** Read the error code (and incident) from a failed response; never throws. */
export async function apiErrorFrom(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as { code?: unknown; error?: { code?: unknown; message?: unknown } };
    const code = body?.error?.code ?? body?.code;
    return new ApiError(res.status, typeof code === "string" ? code : `http_${res.status}`, incidentOf(body?.error?.message));
  } catch {
    return new ApiError(res.status, `http_${res.status}`);
  }
}
