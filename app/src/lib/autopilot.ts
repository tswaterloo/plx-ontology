// The autopilot area's words and views: labels for roles, systems, workflows and states,
// and the filter/split over prepared items (pure, so the views are tested without a page).
import type { AutopilotActionResult, AutopilotArea, AutopilotCountdown, AutopilotItem, AutopilotOptIn, AutopilotScoreBar, AutopilotWorkflowRow } from "./types";
import type { Status } from "./status";

const ROLE: Record<string, string> = {
  purchasing: "Purchasing",
  customer_service: "Customer service",
  receivables: "Receivables",
  payables: "Payables",
  quality: "Quality",
  production_planning: "Production planning",
  rnd: "R&D",
  finance: "Finance",
  owner: "Owner",
};

const SYSTEM: Record<string, string> = {
  mail: "Mail",
  teams: "Teams",
  mc: "Mission Control",
  bc: "Business Central",
  sage: "Sage 300",
  portal: "Portal",
  filemaker: "FileMaker",
  sharepoint: "SharePoint",
  meetings: "Meetings",
  planner: "Planner",
  other: "Other",
};

const WORDS: Record<string, string> = { ar: "AR", ap: "AP", qc: "QC", coa: "CoA", rnd: "R&D", mc: "MC", po: "PO", eta: "ETA" };

const humanize = (id: string) =>
  id
    .split("_")
    .map((w, i) => WORDS[w] ?? (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");

export const roleLabel = (role: string) => ROLE[role] ?? humanize(role);
export const systemLabel = (system: string) => SYSTEM[system] ?? humanize(system);
export const workflowLabel = (workflow: string) => humanize(workflow);

/** A workflow row's state in words, with the status shape it is drawn with. */
export function stateShown(state: AutopilotWorkflowRow["state"], owner: boolean): { status: Status; label: string } {
  switch (state) {
    case "live":
      return { status: "ok", label: "On" };
    case "live_held":
      return { status: "risk", label: "On, held by host switch" };
    case "passed":
      return { status: "new", label: owner ? "Passed, nobody has it on" : "Passed, not on" };
    case "off":
      return { status: "late", label: "Switched off" };
    default:
      return { status: "neutral", label: "Dry run" };
  }
}

/** A row's state in words: Draft when it is prepared for its person before its gate. */
export function rowStateShown(row: Pick<AutopilotWorkflowRow, "state" | "draft">, owner: boolean): { status: Status; label: string } {
  if (row.draft && row.state === "dry_run") return { status: "new", label: "Draft" };
  return stateShown(row.state, owner);
}

/** The gate countdown in one line, in judged held-out items (fresh days, or past days never tuned on): "12 of 15 items at ≥90% · 91.7% right
 * so far", and when the role's newest scorer run was not admitted, that it was not and why.
 * Null before the gate admitted any counts for the role. */
export function countdownText(c: AutopilotCountdown | null | undefined): string | null {
  if (!c) return null;
  const need = `${c.need_percent % 1 === 0 ? c.need_percent.toFixed(0) : c.need_percent}%`;
  const items = c.judged < c.need_judged ? `${c.judged} of ${c.need_judged} items at ≥${need}` : `${c.judged} items (${c.need_judged} needed) at ≥${need}`;
  const pct = c.percent == null ? "none judged yet" : `${c.percent.toFixed(1)}% right so far`;
  const refused = c.refused ? ` · newest scorer run not counted (${c.refused.replace(/_/g, " ")}): these counts are from an earlier run` : "";
  return `${items} · ${pct}${c.passes ? ": meets the gate" : ""}${refused}`;
}

/** Each scored window the gate counts, in words: "Fresh days 6 Nov to 10 Nov: 14 of 15 right"
 * or "Past days never tuned on, 8 Jun to 15 Jun: ...". */
export function countdownParts(c: AutopilotCountdown | null | undefined, date: (iso: string) => string): string[] {
  const out: string[] = [];
  for (const p of c?.parts ?? []) {
    const w = p.window ?? [];
    if (w.length !== 2) continue;
    const span = `${p.kind === "fresh" ? "Fresh days" : "Past days never tuned on,"} ${date(w[0])} to ${date(w[1])}`;
    out.push(`${span}: ${p.judged ? `${p.matched} of ${p.judged} right` : "none judged"}`);
  }
  return out;
}

export const ITEM_STATE: Record<AutopilotItem["state"], string> = {
  waiting: "Waiting",
  edited: "Edited",
  snoozed: "Snoozed",
  accepted: "Accepted",
  rejected: "Rejected",
};

export type Filters = {
  person: string; // "" = everyone
  role: string;
  workflow: string;
  system: string;
  state: string; // "open" = waiting + edited
  today: boolean;
};

export const NO_FILTERS: Filters = { person: "", role: "", workflow: "", system: "", state: "open", today: true };

export type Split = "" | "person" | "role" | "workflow" | "system" | "state";

export function filterItems(area: AutopilotArea, f: Filters): AutopilotItem[] {
  return (area.items ?? []).filter(
    (i) =>
      (!f.person || i.owner === f.person) &&
      (!f.role || (i.roles ?? []).includes(f.role)) &&
      (!f.workflow || i.workflow === f.workflow) &&
      (!f.system || i.system === f.system) &&
      (f.state === "" || (f.state === "open" ? i.state === "waiting" || i.state === "edited" : i.state === f.state)) &&
      (!f.today || i.today),
  );
}

/** A person's name; "You" for the reader. Someone with no name on record (the role roster
 * holds ids, never names) is called by their first role, numbered when two read the same. */
export function personName(area: AutopilotArea, id: string): string {
  const people = area.people ?? [];
  const p = people.find((x) => x.id === id);
  if (!p) return "Someone else";
  if (p.you) return "You";
  if (p.name) return p.name;
  const base = (x: { roles?: string[] }) => (x.roles?.[0] ? `${roleLabel(x.roles[0])} colleague` : "Colleague");
  const same = people.filter((x) => !x.you && !x.name && base(x) === base(p));
  return same.length > 1 ? `${base(p)} ${same.indexOf(p) + 1}` : base(p);
}

/** The items in groups by `split` (one unnamed group without a split), largest first. */
export function splitItems(area: AutopilotArea, items: AutopilotItem[], split: Split): { key: string; label: string; items: AutopilotItem[] }[] {
  if (!split) return [{ key: "", label: "", items }];
  const groups = new Map<string, AutopilotItem[]>();
  for (const i of items) {
    const keys = split === "role" ? (i.roles?.length ? i.roles : [""]) : [split === "person" ? i.owner : split === "state" ? i.state : i[split]];
    for (const k of keys) groups.set(k, [...(groups.get(k) ?? []), i]);
  }
  const label = (k: string) =>
    split === "person" ? personName(area, k) : split === "role" ? (k ? roleLabel(k) : "No role") : split === "system" ? systemLabel(k) : split === "state" ? ITEM_STATE[k as AutopilotItem["state"]] ?? k : workflowLabel(k);
  return [...groups.entries()].map(([key, list]) => ({ key, label: label(key), items: list })).sort((a, b) => b.items.length - a.items.length || a.label.localeCompare(b.label));
}

/** The workflow rows a view shows: a person's own role and the roles they belong to; a role's rows. */
export function filterWorkflows(area: AutopilotArea, f: Filters): AutopilotWorkflowRow[] {
  const person = f.person ? (area.people ?? []).find((p) => p.id === f.person) : null;
  return (area.workflows ?? []).filter(
    (w) =>
      (!f.person || w.person === f.person || (person?.roles ?? []).includes(w.role)) &&
      (!f.role || w.role === f.role) &&
      (!f.workflow || w.workflow === f.workflow),
  );
}

/** Whether accepting this item performs it live: the server says so (the owner opened its
 * kind to approved writes, or its workflow is live), or a live row reaches the item's owner. */
export function itemLive(area: AutopilotArea, item: AutopilotItem): boolean {
  return item.live === true || (area.workflows ?? []).some(
    (w) => w.workflow === item.workflow && w.state === "live" && (w.person === item.owner || (item.roles ?? []).includes(w.role)),
  );
}

/** A row's role in words: a person role is the person's name. */
export function rowRole(area: AutopilotArea, row: AutopilotWorkflowRow): string {
  return row.person ? personName(area, row.person) : row.role.startsWith("person:") ? "One person" : roleLabel(row.role);
}

export const pct = (s: { percent?: number | null; judged: number } | null | undefined) =>
  s && s.percent != null ? `${s.percent.toFixed(1)}%` : "—";

// ---- the studio: modes and due words -------------------------------------------------

export type Mode = "off" | "suggest" | "draft" | "auto" | "watch";

export const MODES: { mode: Mode; label: string; words: string }[] = [
  { mode: "off", label: "Off", words: "Nothing is prepared, for every person and role of this workflow." },
  { mode: "suggest", label: "Suggest", words: "Says what is open and what to do next, learned from your history." },
  { mode: "draft", label: "Draft", words: "Prepares the work; you edit and approve each one." },
  { mode: "auto", label: "Auto", words: "Acts on your click, after its preview." },
  { mode: "watch", label: "Watch", words: "Acts by itself and shows what it did, with undo. Only after its gate passed." },
];

/** A row's mode: off (the owner stopped it), auto (someone it acts for switched it on for
 * themselves), else draft. */
export function rowMode(row: AutopilotWorkflowRow): Mode {
  if (!row.on) return "off";
  return (row.switched_on ?? 0) > 0 ? "auto" : "draft";
}

/** Whether a row's held-out gate passed (it may go Auto, or Watch where the server has it). */
export const rowPassed = (row: AutopilotWorkflowRow) => row.state !== "dry_run" && row.state !== "off" && row.fresh != null;

/** The modes the owner may set on a row: off and draft; suggest where the server has the
 * builder. Never auto or watch: those act for a person, and only that person switches them
 * on (the Autopilot page's own switches). */
export function modesFor(_row: AutopilotWorkflowRow, builder: boolean): Set<Mode> {
  const out = new Set<Mode>(["off", "draft"]);
  if (builder) out.add("suggest");
  return out;
}

// ---- a person's own switches ----------------------------------------------------------

const num = (n: number) => n.toLocaleString("en-CA");
const pctOf = (p: number) => `${p % 1 === 0 ? p.toFixed(0) : p}%`;

/** A workflow's score against its bar, in one line: "Eligible: 1,243 of 1,292 past items
 * matched what you did (96.2%). It needs 90% on at least 15." */
export function optInScore(o: Pick<AutopilotOptIn, "eligible" | "score">): string {
  const s = o.score;
  if (!s) return o.eligible ? "Eligible." : "Not eligible: no score on record.";
  const got = `${num(s.matched)} of ${num(s.judged)} past items matched what you did${s.percent == null ? "" : ` (${s.percent.toFixed(1)}%)`}`;
  return `${o.eligible ? "Eligible" : "Not eligible"}: ${got}. It needs ${pctOf(s.need_percent)} on at least ${num(s.need_judged)}.`;
}

/** Why an opt-in was switched off by itself, in plain words. */
export function suspendedWords(reason: string, score: AutopilotScoreBar | null | undefined, when: string): string {
  const need = score ? `${pctOf(score.need_percent)} on at least ${num(score.need_judged)} items` : "its bar";
  const why =
    reason === "below_bar" && score?.percent != null
      ? `its score fell to ${score.percent.toFixed(1)}% on ${num(score.judged)} items, under the ${need} it needs`
      : reason === "too_few" && score
        ? `only ${num(score.judged)} items are checked now, and it needs ${need}`
        : reason === "no_score"
          ? "its score is no longer on record"
          : "it no longer passes its check";
  return `Turned off by itself${when ? ` on ${when}` : ""}: ${why}. Turn it on again once it passes.`;
}

/** "in 40 min", "in 3 h", "2 h overdue", "3 days overdue". */
export function dueWords(iso: string, now: Date = new Date()): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const mins = Math.round((t - now.getTime()) / 60_000);
  const span = (m: number) => (m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`);
  if (mins >= 0) return `in ${span(Math.max(1, mins))}`;
  return `${span(-mins)} overdue`;
}

export type OutcomeStep = AutopilotActionResult;

const NOT_RUN = new Set(["dry_run", "needs_confirm", "killed", "refused", "excluded", "not_routine", "unavailable", "rate_limited", "duplicate", "not_performed"]);
const words = (rest: string[]) => rest.slice(0, 2).join(", ").replace(/_/g, " ");
const sends = (kind: string) => kind === "send_mail" || kind === "send_teams";
const stepName = (s: OutcomeStep) =>
  s.kind === "send_mail" ? "the email" : s.kind === "send_teams" ? "the Teams message" : `the ${systemLabel(s.system)} write`;

/** One step in words: went through, not confirmed, failed, or never ran and why. */
function stepWords(s: OutcomeStep): string {
  const name = stepName(s);
  const why = s.reasons?.length ? ` (${words(s.reasons)})` : "";
  if (s.status === "done") return `${name} went through`;
  if (s.status === "failed") return `${name} failed${why}`;
  if (NOT_RUN.has(s.status)) return `${name} never ran (${words([s.status, ...(s.reasons ?? [])])}), so it was not ${sends(s.kind) ? "sent" : "made"}`;
  return `could not confirm whether ${name} went through`;
}

const sentence = (parts: string[]) => {
  const text = parts.join("; ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

/**
 * What an accept did, over all of the item's actions. Only a refusal before anything ran
 * says nothing was sent or written. With the decision's steps it names each one: which went
 * through, which failed and which never ran; it says to check a system only for a step that
 * could not be confirmed there, never for one that was confirmed or never ran.
 */
export function outcomeWords(outcome: string, reasons: string[], system: string, steps: OutcomeStep[] = []): string {
  const unsure = steps.filter((s) => s.status !== "done" && s.status !== "failed" && !NOT_RUN.has(s.status));
  const check = unsure.length ? ` Check ${[...new Set(unsure.map((s) => systemLabel(s.system)))].join(" and ")} before trying again.` : "";
  switch (outcome) {
    case "dry_run":
      return ". Dry run: your decision is recorded, nothing was sent or written.";
    case "done":
      return ". Done live through the write path.";
    case "killed":
      return ". Recorded; switched off, so nothing was performed.";
    case "unknown":
    case "pending":
      if (steps.length) return `. ${sentence(steps.map(stepWords))}.${check}`;
      return `. Could not confirm whether it went through in ${system}. Check there before trying again.`;
    case "partly_done":
      if (steps.length) return `. Partly done: ${steps.map(stepWords).join("; ")}.${check}`;
      return `. Partly done: a step went through, another did not${reasons.length ? ` (${words(reasons)})` : ""}.`;
    case "failed":
      if (steps.length) return `. ${sentence(steps.map(stepWords))}.${check}`;
      return `. The write failed${reasons.length ? ` (${words(reasons)})` : ""}; nothing after it was done.`;
    default:
      if (!NOT_RUN.has(outcome)) return `. Could not confirm what happened (${words([outcome])}). Check ${system} before trying again.`;
      return `. Recorded; the write path would not perform it (${words([outcome, ...reasons])}), so nothing was sent or written.`;
  }
}
