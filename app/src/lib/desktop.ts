// The desktop app (desktop/): the same Workbench in its own window. It gives the page a small
// bridge, `window.plxDesktop`, for system notifications and for its menu; a browser has none.
// Nothing here changes what a reader may see: notifications are built from reads the page
// already makes as that reader.

export interface DesktopNotice {
  title: string;
  body?: string;
  /** A path inside the Workbench that a click opens. */
  href?: string;
}

export interface PlxDesktop {
  app: true;
  version: string;
  notify: (n: DesktopNotice) => void;
  onMenu: (cb: (msg: unknown) => void) => () => void;
}

/** The bridge when this page is running inside the desktop app, else null. */
export function desktopBridge(): PlxDesktop | null {
  if (typeof window === "undefined") return null;
  const d = (window as unknown as { plxDesktop?: PlxDesktop }).plxDesktop;
  return d && d.app === true && typeof d.notify === "function" && typeof d.onMenu === "function" ? d : null;
}

const MENU_HREF: Record<string, string> = { home: "/", ask: "/ask", "my-work": "/my-work", autopilot: "/autopilot" };

export type MenuTarget = { kind: "feedback" } | { kind: "go"; href: string };

/** What a message from the desktop shell asks the page to do; anything unknown asks nothing. */
export function menuTarget(msg: unknown): MenuTarget | null {
  if (!msg || typeof msg !== "object") return null;
  const m = msg as { action?: unknown; href?: unknown };
  if (m.action === "feedback") return { kind: "feedback" };
  if (typeof m.action === "string" && Object.prototype.hasOwnProperty.call(MENU_HREF, m.action)) return { kind: "go", href: MENU_HREF[m.action] };
  // a clicked notification: a path of this app, never another site
  if (typeof m.href === "string" && /^\/(?!\/)[^\s\\]*$/.test(m.href)) return { kind: "go", href: m.href };
  return null;
}

export interface Pending {
  /** Stable per thing (an alarm key, an autopilot item id). */
  id: string;
  title: string;
  body?: string;
  href?: string;
}

/**
 * Which of `now` to tell the person about: those not in `seen`. The first read (seen is null)
 * only records what was already there, so opening the app never fires a burst.
 */
export function freshOnes(seen: ReadonlySet<string> | null, now: Pending[]): Pending[] {
  if (seen === null) return [];
  return now.filter((p) => !seen.has(p.id));
}

/** At most `max` notifications at once; the rest are said as one line. */
export function capNotices(list: Pending[], max = 3): DesktopNotice[] {
  const shown: DesktopNotice[] = list.slice(0, max).map((p) => ({ title: p.title, body: p.body, href: p.href }));
  if (list.length > max) shown.push({ title: `${list.length - max} more need you`, href: "/my-work" });
  return shown;
}
