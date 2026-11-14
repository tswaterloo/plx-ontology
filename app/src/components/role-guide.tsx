"use client";

import Link from "next/link";
import { useFeedbackDialog } from "@/components/feedback-dialog";
import { workflowLabel } from "@/lib/autopilot";
import { AUTOPILOT_STEPS, FEEDBACK_STEPS, type RoleGuide as Guide } from "@/lib/onboarding";

/** Autopilot workflow names as people see them on the Autopilot page (registry labels). */
const AUTOPILOT_NAMES: Record<string, string> = {
  customer_po: "Customer PO received",
  customer_request: "Customer asks for status or documents",
  reply_prep: "Someone is waiting on your reply",
  po_followup: "Supplier PO without confirmation",
  supplier_eta: "Supplier states a ship date",
  invoice_entry: "Shipment to invoice",
  cash_application: "Remittance to apply",
  task_assigned: "Task assigned",
  meeting_followup: "Meeting ended",
  qc_hold: "QC hold or fail",
  approval_request: "Approval requested",
  mail_approval_request: "Approval asked in mail",
};
export const autopilotName = (id: string) => AUTOPILOT_NAMES[id] ?? workflowLabel(id);

function Section({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="space-y-1.5">
      <h2 id={id} className="text-[13px] font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

const list = "list-disc space-y-1 pl-4 text-[12px] text-muted";

/** One role's first-week guide: what the Workbench does for it, how to ask, autopilot and feedback. */
export function RoleGuide({ guide }: { guide: Guide }) {
  const feedback = useFeedbackDialog();
  return (
    <div className="space-y-5" data-testid="role-guide">
      <Section id="guide-does" title="What the Workbench does for you">
        <ul className={list}>
          {guide.does.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
        <p className="text-[12px] text-muted">
          It only reads. It never sends a message, changes an order or approves anything for you. Pages to try this week:{" "}
          {[{ href: "/", label: "My work" }, { href: "/ask", label: "Ask" }, ...guide.pages].map((p, i, all) => (
            <span key={p.href}>
              <Link href={p.href} className="underline">
                {p.label}
              </Link>
              {i < all.length - 1 ? ", " : "."}
            </span>
          ))}
        </p>
      </Section>

      <Section id="guide-ask" title="How to ask">
        <p className="text-[12px] text-muted">
          Type into Ask the way you would write to a colleague. Use numbers the way you write them in mail; prefixes are optional. Put in your own order, supplier or lot in place of the examples below. Every answer lists the mail, records and files it came from. When the Workbench cannot find enough, it says so and does not guess.
        </p>
        <ul className={list} data-testid="guide-asks">
          {guide.asks.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      </Section>

      <Section id="guide-autopilot" title="Autopilot">
        <ul className={list} data-testid="guide-autopilot">
          {AUTOPILOT_STEPS.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
        <p className="text-[12px] text-muted">Workflows that prepare routine items for this role:</p>
        <ul className={list}>
          {guide.autopilots.map((a) => (
            <li key={a}>{autopilotName(a)}</li>
          ))}
        </ul>
        <p className="text-[12px] text-muted">
          A workflow shows a Turn on switch only once it has passed its check for you. See{" "}
          <Link href="/autopilot" className="underline">
            Autopilot
          </Link>
          .
        </p>
      </Section>

      <Section id="guide-feedback" title="Feedback">
        <ul className={list}>
          {FEEDBACK_STEPS.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
        <button type="button" onClick={feedback.open} className="text-[12px] font-medium underline cursor-pointer" data-testid="guide-feedback">
          Give feedback now
        </button>
      </Section>
    </div>
  );
}
