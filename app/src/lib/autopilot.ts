// The autopilot area's words and views: labels for roles, systems, workflows and states,
// and the filter/split over prepared items (pure, so the views are tested without a page).
import type { AutopilotArea, AutopilotItem, AutopilotWorkflowRow } from "./types";
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
      return { status: "ok", label: "Live" };
    case "live_held":
      return { status: "risk", label: "Live, held by host switch" };
    case "passed_awaiting_owner":
      return { status: "new", label: owner ? "Passed, awaiting you" : "Passed, awaiting owner" };
    case "off":
      return { status: "late", label: "Switched off" };
    default:
      return { status: "neutral", label: "Dry run" };
  }
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

/** Whether accepting this item performs it live (a live row reaches the item's owner). */
export function itemLive(area: AutopilotArea, item: AutopilotItem): boolean {
  return (area.workflows ?? []).some(
    (w) => w.workflow === item.workflow && w.state === "live" && (w.person === item.owner || (item.roles ?? []).includes(w.role)),
  );
}

/** A row's role in words: a person role is the person's name. */
export function rowRole(area: AutopilotArea, row: AutopilotWorkflowRow): string {
  return row.person ? personName(area, row.person) : row.role.startsWith("person:") ? "One person" : roleLabel(row.role);
}

export const pct = (s: { percent?: number | null; judged: number } | null | undefined) =>
  s && s.percent != null ? `${s.percent.toFixed(1)}%` : "—";
