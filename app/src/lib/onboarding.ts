// The first-week guide's words per role and the onboarding checklist's steps (pure, so the
// help page and the checklist are tested without a page). The role ids are the autopilot
// roster's (operating/autopilot_roster.py); each `autopilots` id is an enabled entry of
// operating/autopilot_registry.json (copy.test-style check in onboarding.test.ts).
import { roleLabel } from "./autopilot";

export type GuideRole = "customer_service" | "purchasing" | "receivables" | "rnd" | "quality" | "production_planning" | "finance";

export type RoleGuide = {
  role: GuideRole;
  /** What the Workbench does for this role, one plain sentence each. */
  does: string[];
  /** The record a person in this role looks up most, for the checklist hint. */
  lookUp: string;
  /** Questions typed the way a person in this role asks them. */
  asks: string[];
  /** Autopilot workflows that prepare this role's routine items (registry ids). */
  autopilots: string[];
  /** Pages worth opening in week one besides My work and Ask. */
  pages: { href: string; label: string }[];
};

export const GUIDES: RoleGuide[] = [
  {
    role: "customer_service",
    does: [
      "Shows customer mail that is waiting on you, with the sales order it is about already found.",
      "Puts an order's history in one place: what the customer sent, what we confirmed, what shipped and when.",
      "Drafts the order entry from a customer PO in your inbox so you can check it against the PO side by side.",
      "Answers status questions with the dates from BC and the Portal, and says which one is newer when they differ.",
    ],
    lookUp: "a sales order or customer PO number you are working on",
    asks: ["Where is sales order 10432 and when does it ship?", "What did Acme last ask us about their June order?", "Which of my customer orders are at risk of shipping late?"],
    autopilots: ["customer_po", "customer_request", "reply_prep"],
    pages: [{ href: "/watching", label: "Watching" }],
  },
  {
    role: "purchasing",
    does: [
      "Lists POs the supplier has not confirmed, and POs past their promised date, on Late POs.",
      "Reads supplier mail for new ship dates and puts the date next to the PO it changes.",
      "Shows a supplier's record with open POs, late history and the last mail from each contact.",
      "Drafts the follow-up to a supplier for you to edit and send yourself.",
    ],
    lookUp: "a PO number or a supplier you buy from",
    asks: ["Has the supplier confirmed PO 4512 and when does it ship?", "Which POs from Northfield are late this month?", "What did we last tell the supplier about the resin shortage?"],
    autopilots: ["po_followup", "supplier_eta", "reply_prep"],
    pages: [{ href: "/late", label: "Late POs" }],
  },
  {
    role: "receivables",
    does: [
      "Shows shipments that have no invoice yet, with the order and the shipping papers behind each one.",
      "Matches a customer remittance to the invoices it pays and shows what is left unmatched.",
      "Puts a customer's open invoices, promises to pay and the last collections mail on one page.",
      "Drafts the reply to an invoice question for you to check and send yourself.",
    ],
    lookUp: "an invoice number or a customer with open invoices",
    asks: ["Which invoices does the remittance from Acme on Monday cover?", "What has Brightline promised to pay this month?", "Which shipments from last week have no invoice yet?"],
    autopilots: ["invoice_entry", "cash_application", "reply_prep"],
    pages: [{ href: "/watching", label: "Watching" }],
  },
  {
    role: "rnd",
    does: [
      "Lists R&D and sample projects with who is waiting on whom and since when.",
      "Finds the mail, specs and test results behind a project, including files on SharePoint you can open.",
      "Shows which samples went out, to which customer, and what they said back.",
      "Reminds you of tasks assigned to you in Planner and Mission Control.",
    ],
    lookUp: "a sample project, a formula code or a customer you are developing for",
    asks: ["What is the status of the sample project for Acme's new coating?", "Which samples have we sent this month with no reply?", "Where is the latest spec sheet for formula 2207?"],
    autopilots: ["task_assigned", "reply_prep", "meeting_followup"],
    pages: [{ href: "/rd-projects", label: "R&D projects" }],
  },
  {
    role: "quality",
    does: [
      "Shows lots on QC hold or failed, with the test results and the orders that wait on them.",
      "Finds the CoA and certificates a customer asks for and shows which ones are missing.",
      "Puts a lot's tests, release and shipments in one timeline.",
      "Lists customer requests for documents that are waiting on you.",
    ],
    lookUp: "a lot number or a product you release",
    asks: ["Which lots are on hold and which orders are waiting on them?", "Do we have a CoA for lot 24-0815?", "What did the lab find on the last three batches of product 330?"],
    autopilots: ["qc_hold", "customer_request", "reply_prep"],
    pages: [{ href: "/watching", label: "Watching" }],
  },
  {
    role: "production_planning",
    does: [
      "Shows which orders are short of material and which POs would cover them.",
      "Puts supplier ship dates next to the production dates that depend on them.",
      "Lists late POs for materials you plan with, by supplier.",
      "Drafts a short summary after a meeting you organized, with who agreed to do what, for you to send.",
    ],
    lookUp: "a sales order you are planning or a raw material",
    asks: ["Which orders next week are short of material?", "When does the resin on PO 4512 arrive and which orders need it?", "What changed in the production plan since Monday?"],
    autopilots: ["supplier_eta", "task_assigned", "meeting_followup"],
    pages: [
      { href: "/late", label: "Late POs" },
      { href: "/meetings", label: "Meetings" },
    ],
  },
  {
    role: "finance",
    does: [
      "Shows approvals waiting on you, from Mission Control and from mail, with the record each one is about.",
      "Puts a supplier invoice next to the PO and receipt it bills, and points out when the quantities differ.",
      "Shows a customer's or supplier's open balance and the mail behind it.",
      "Keeps payroll, HR and personal finance files hidden unless your role covers them.",
    ],
    lookUp: "a supplier invoice or a customer with an open balance",
    asks: ["Which approvals are waiting on me?", "Does the Northfield invoice for PO 4512 match what we received?", "What is Acme's open balance and when did they last pay?"],
    autopilots: ["approval_request", "mail_approval_request", "cash_application"],
    pages: [{ href: "/watching", label: "Watching" }],
  },
];

export const guideFor = (role: string): RoleGuide | null => GUIDES.find((g) => g.role === role) ?? null;
export const guideLabel = (role: GuideRole) => roleLabel(role);

/** How autopilot opt-in works, the same for every role. The bar is the gate's precision in
 * operating/autopilot_write_rules.json (0.9). */
export const AUTOPILOT_STEPS: string[] = [
  "Autopilot is off for you when you start. Nobody else can turn it on for you, the owner included.",
  "Each workflow is switched on one at a time, by you, on the Autopilot page under Your autopilot.",
  "A workflow can be turned on only after it matched what you did yourself on enough of your past items, at least 90% of them.",
  "Once on, it handles routine items only. Money, prices, credit, permissions and new customers or suppliers always come to you.",
  "If its score drops below 90%, it turns itself off and tells you why. You can turn it on again once it passes.",
  "Pause everything stops all of yours at once. Turn off stops one.",
];

export const FEEDBACK_STEPS: string[] = [
  "Press F, or choose Give feedback in the menu under your name.",
  "Say what you expected and what you saw. One line is enough.",
  "Every answer and record also has a thumbs up and down. Use them when an answer is wrong.",
  "You can see what happened to your notes on the Feedback list.",
];

export type Step = { id: string; label: string; hint: string; href?: string };

/** The week-one checklist for a role: the same six steps, with the role's own record and question. */
export function checklistFor(role: GuideRole | null): Step[] {
  const g = role ? guideFor(role) : null;
  return [
    { id: "sign_in", label: "Sign in with Microsoft", hint: "Use your usual PLX account. There is no separate password." },
    { id: "my_work", label: "Read today's My work", hint: "Open an item to see why it is listed.", href: "/" },
    { id: "look_up", label: "Look up a record", hint: `Type ${g ? g.lookUp : "an order, PO, lot or invoice number"} into Search.` },
    { id: "ask", label: "Ask one question", hint: g ? `For example: ${g.asks[0]}` : "Ask in your own words. The answer lists its sources.", href: "/ask" },
    { id: "autopilot", label: "Look at your autopilot switches", hint: "They are all off. Leave them off until you have used the Workbench for a few days.", href: "/autopilot" },
    { id: "feedback", label: "Send one feedback note", hint: "Tell us one thing that was wrong, missing or slow." },
  ];
}

/** Where a reader's ticked steps are kept (this browser only). */
export const checklistKey = (principal: string) => `plx.onboarding.done.${principal}`;
/** Where a reader's chosen role on the help page is kept (this browser only). */
export const roleKey = (principal: string) => `plx.onboarding.role.${principal}`;

export function readDone(principal: string): string[] {
  try {
    const raw = window.localStorage.getItem(checklistKey(principal));
    const v: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function writeDone(principal: string, done: string[]): void {
  try {
    window.localStorage.setItem(checklistKey(principal), JSON.stringify(done));
  } catch {
    /* private mode: the ticks last until the page closes */
  }
}
