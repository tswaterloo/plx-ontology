"use client";

import Link from "next/link";
import { useState } from "react";
import { CardActions } from "@/components/card-actions";
import { recordName } from "@/components/work-log";
import { formatDate, objectHref, plural } from "@/lib/format";
import type { ActionResult, RdBoard, RdProject } from "@/lib/types";

const KIND: Record<string, string> = {
  sample_request: "Sample request",
  formulation_change: "Formulation change",
  stability_test: "Stability test",
  coa: "Certificate of analysis",
};
const STATUS: Record<string, string> = {
  waiting_on_us: "Waiting on us",
  waiting_on_customer: "Waiting on the customer",
  update_received: "Update received",
  closed: "Closed",
};

/** The board's projects per customer, the customer with the most idle (then open) work first,
 *  each group headed by the customer's name as you read its record (its handle when the
 *  server gave none), then named before unnamed, then by name. */
export function byCustomer(projects: RdProject[]): { customer: string; name: string; named: boolean; projects: RdProject[] }[] {
  const groups = new Map<string, RdProject[]>();
  for (const p of projects) {
    if (typeof p.customer !== "string" || !p.customer) continue; // a project always names its customer
    groups.set(p.customer, [...(groups.get(p.customer) ?? []), p]);
  }
  const weight = (ps: RdProject[]) => [ps.filter((p) => p.idle).length, ps.filter((p) => p.status !== "closed").length];
  return [...groups.entries()]
    .map(([customer, ps]) => {
      const name = ps.find((p) => p.customer_name)?.customer_name;
      return { customer, name: name || recordName({ object_id: customer }), named: !!name, projects: ps };
    })
    .sort((a, b) => {
      const [ia, oa] = weight(a.projects);
      const [ib, ob] = weight(b.projects);
      return ib - ia || ob - oa || Number(b.named) - Number(a.named) || a.name.localeCompare(b.name) || a.customer.localeCompare(b.customer);
    });
}

// R&D and sample projects per customer, built only from the threads and records you may open.
// A thread of a supplier, or of a company no customer record names, is no project. A status
// update is a draft: it goes out only through the card's preview and your confirm.
export function RdBoardView({ board }: { board: RdBoard }) {
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const groups = byCustomer(board.projects ?? []);
  return (
    <div className="space-y-6" data-testid="rd-board">
      {groups.length === 0 ? (
        <p className="text-[13px] text-muted">No R&amp;D or sample projects with a customer in the mail you can open.</p>
      ) : (
        <p className="text-[13px] text-fg">
          {plural(board.total, "project")} for {plural(groups.length, "customer")}, {board.idle} idle (no activity for {board.idle_after_days}+ business days).
        </p>
      )}
      <p className="text-[12px] text-muted">
        Read from mail. A project belongs to the customer its mail is linked to, or whose record states the sender&apos;s address; supplier threads are left out. Status updates are drafts: nothing is sent until you confirm it.
      </p>
      {groups.map((g) => (
        <section key={g.customer} aria-label={g.name} className="space-y-2" data-testid="rd-customer">
          <h2 className="flex items-baseline justify-between gap-3 text-[14px] font-semibold">
            <Link className="hover:underline" href={objectHref(g.customer.split(":")[0], g.customer)}>
              {g.name}
            </Link>
            <span className="text-[12px] font-normal text-muted">{plural(g.projects.length, "project")}</span>
          </h2>
          <ul className="space-y-3">
            {g.projects.map((p: RdProject) => (
              <li key={p.id} className="space-y-1 border-l-2 border-border pl-3 text-[13px]" data-testid="rd-project">
                <div>
                  <span className="font-medium">{KIND[p.kind] ?? p.kind}</span>
                  {p.idle && <span className="ml-2 text-[12px] text-[color:var(--late)]" data-testid="rd-idle">Idle</span>}
                </div>
                <div className="text-muted">
                  {STATUS[p.status] ?? p.status}
                  {p.waiting_on && !(STATUS[p.status] ?? "").startsWith("Waiting on") ? ` · waiting on ${p.waiting_on === "us" ? "us" : "the customer"}` : ""}
                  {" · "}
                  {p.last_on ? `last activity ${formatDate(p.last_on, { short: true })} (${plural(p.days, "business day")} ago)` : "no activity dated"}
                  {p.owner_is_you ? " · yours" : p.owner ? " · someone else owns it" : " · no owner"}
                </div>
                <ul className="list-disc pl-5 text-muted">
                  {(p.lines ?? []).map((l) => (
                    <li key={l.id}>{l.subject || "(no subject)"} · {STATUS[l.state] ?? l.state} · {plural(l.messages_n, "message")}</li>
                  ))}
                </ul>
                {p.draft && <p className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]" data-testid="rd-draft">{p.draft.body}</p>}
                {p.card_id && (
                  <CardActions
                    card={{ id: p.card_id, subject: { label: p.draft?.subject ?? "Project status" }, actions: p.offers }}
                    state={states[p.card_id] ?? null}
                    onDone={(r) => setStates((s) => ({ ...s, [p.card_id as string]: r }))}
                  />
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
