"use client";

// A record page's Impact panel: what a change puts at risk downstream (what if), or why an
// order is late or unprofitable. Read on demand, as the reader reads it; nothing is written.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { getImpact, type ImpactChange } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDate, money, safeHref } from "@/lib/format";
import type { ImpactPanel as Panel } from "@/lib/types";
import { SectionTitle } from "./record/parts";

/** The changes a record type can be asked about (the flow's stages, ontology/impact_flows.json). */
const SUPPLY = new Set(["Supplier", "PurchaseOrder", "PurchaseOrderLine"]);
const ITEMS = new Set(["Product"]);
const ORDERS = new Set(["SalesOrder"]);
const CUSTOMERS = new Set(["Customer"]);

export function impactKinds(type: string): ImpactChange["kind"][] {
  if (SUPPLY.has(type)) return ["delay", "price_change"];
  if (ITEMS.has(type)) return ["out_of_stock", "price_change"];
  if (CUSTOMERS.has(type)) return ["customer_lost", "price_change"];
  return [];
}

export function impactApplies(type: string): boolean {
  return impactKinds(type).length > 0 || ORDERS.has(type);
}

const KIND_WORDS: Record<ImpactChange["kind"], string> = {
  delay: "Delayed",
  out_of_stock: "Out of stock",
  customer_lost: "Customer lost",
  price_change: "Price change",
};

const MEASURE_WORDS: Record<NonNullable<Panel["money"]>[number]["measure"], string> = {
  revenue_at_risk: "Revenue at risk",
  revenue_lost: "Revenue lost",
  revenue_change: "Revenue change",
  cost_change: "Cost change",
};

const CAUSE_WORDS: Record<NonNullable<Panel["causes"]>[number]["cause"], string> = {
  late_supply: "Late supply",
  supplier_no_reply: "No supplier reply",
  short_receipt: "Short receipt",
  qc_hold: "QC hold",
  cost_jump: "Cost jump",
  freight: "Freight",
};

function Tier({ tier }: { tier: "firm" | "lead" }) {
  return tier === "lead" ? <span className="ml-1.5 rounded bg-amber-50 px-1 text-[11px] text-amber-700">possible</span> : null;
}

export function buildChange(kind: ImpactChange["kind"], days: number, percent: string): ImpactChange | null {
  if (kind === "delay") return Number.isFinite(days) && days >= 1 && days <= 365 ? { kind, days: Math.round(days) } : null;
  if (kind === "price_change") return /^-?\d{1,4}(\.\d{1,2})?$/.test(percent) && Number(percent) !== 0 ? { kind, percent } : null;
  return { kind };
}

export function ImpactCard({ type, id }: { type: string; id: string }) {
  const kinds = impactKinds(type);
  const why = ORDERS.has(type);
  const [kind, setKind] = useState<ImpactChange["kind"] | null>(kinds[0] ?? null);
  const [days, setDays] = useState(7);
  const [percent, setPercent] = useState("5");
  const [panel, setPanel] = useState<Panel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);

  const run = useCallback(() => {
    const change = !why && kind ? buildChange(kind, days, percent) : undefined;
    if (!why && !change) {
      setError("Enter a valid change first.");
      return;
    }
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setBusy(true);
    setError(null);
    getImpact(why ? "why" : "what_if", type, id, change ?? undefined, ctl.signal)
      .then((p) => setPanel(p))
      .catch((e) => {
        if (!ctl.signal.aborted) setError(plainError(e));
      })
      .finally(() => {
        if (!ctl.signal.aborted) setBusy(false);
      });
  }, [why, kind, days, percent, type, id]);

  useEffect(() => () => abort.current?.abort(), []);

  if (!impactApplies(type)) return null;
  return (
    <section data-testid="impact">
      <SectionTitle>{why ? "Why" : "Impact"}</SectionTitle>
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        {!why && (
          <>
            <select aria-label="Change" className="rounded border px-1.5 py-1" value={kind ?? ""} onChange={(e) => setKind(e.target.value as ImpactChange["kind"])}>
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {KIND_WORDS[k]}
                </option>
              ))}
            </select>
            {kind === "delay" && (
              <label className="flex items-center gap-1">
                <input aria-label="Days" type="number" min={1} max={365} className="w-16 rounded border px-1.5 py-1" value={days} onChange={(e) => setDays(Number(e.target.value))} />
                days
              </label>
            )}
            {kind === "price_change" && (
              <label className="flex items-center gap-1">
                <input aria-label="Percent" className="w-16 rounded border px-1.5 py-1" value={percent} onChange={(e) => setPercent(e.target.value.trim())} />%
              </label>
            )}
          </>
        )}
        <button type="button" className="rounded border px-2 py-1 hover:bg-muted/10 disabled:opacity-50" onClick={run} disabled={busy}>
          {busy ? "Walking links…" : why ? "Find causes" : "Show impact"}
        </button>
      </div>
      {error && <p className="mt-2 text-[13px] text-red-700">{error}</p>}
      {panel && <ImpactBody panel={panel} />}
    </section>
  );
}

function RecordLink({ r }: { r: { open_url: string; label: string } }) {
  const href = safeHref(r.open_url);
  return href ? (
    <Link href={href} className="hover:underline">
      {r.label}
    </Link>
  ) : (
    <span>{r.label}</span>
  );
}

export function ImpactBody({ panel: raw }: { panel: Panel }) {
  const panel = { ...raw, money: raw.money ?? [], orders: raw.orders ?? [], customers: raw.customers ?? [], causes: raw.causes ?? [], unknown: raw.unknown ?? [], orders_total: raw.orders_total ?? 0 };
  if (!panel.applies) return <p className="mt-2 text-[13px] text-muted">No impact walk applies to this record.</p>;
  return (
    <div className="mt-3 space-y-3 text-[13px]" data-testid="impact-body">
      {panel.money.length > 0 && (
        <ul className="space-y-1">
          {panel.money.map((m, i) => (
            <li key={i}>
              <span className="text-muted">{MEASURE_WORDS[m.measure]}: </span>
              <span className="font-medium tabular-nums">{money(m.money)}</span>
              <span className="text-muted"> over {m.orders} order{m.orders === 1 ? "" : "s"}</span>
              <Tier tier={m.tier} />
            </li>
          ))}
        </ul>
      )}
      {panel.mode === "what_if" && (
        <div>
          <p className="text-muted">
            {panel.orders_total === 0 ? "No open customer order is reached through links you can read." : `${panel.orders_total} customer order${panel.orders_total === 1 ? "" : "s"} reached${panel.change ? `, ${panel.change}` : ""}.`}
          </p>
          <ul className="mt-1 space-y-1">
            {panel.orders.map((o) => (
              <li key={o.order.object_id} className="flex flex-wrap items-baseline gap-x-2">
                <RecordLink r={o.order} />
                <Tier tier={o.tier} />
                {o.required_date && <span className="text-muted">required {formatDate(o.required_date)}</span>}
                {o.slips && o.late_by_days != null && <span className="text-red-700">slips {o.late_by_days} day{o.late_by_days === 1 ? "" : "s"}</span>}
                {o.slips === false && <span className="text-muted">still on time</span>}
                {o.revenue && <span className="tabular-nums">{money(o.revenue)}</span>}
              </li>
            ))}
          </ul>
          {panel.customers.length > 0 && (
            <p className="mt-1">
              <span className="text-muted">Customers: </span>
              {panel.customers.map((c, i) => (
                <span key={c.object_id}>
                  {i > 0 && ", "}
                  <RecordLink r={c} />
                </span>
              ))}
            </p>
          )}
        </div>
      )}
      {panel.mode === "why" && (
        <ul className="space-y-1.5">
          {panel.causes.length === 0 && <li className="text-muted">No cause is visible in the linked records you can read.</li>}
          {panel.causes.map((c, i) => (
            <li key={i}>
              <span className="font-medium">{CAUSE_WORDS[c.cause]}</span>
              <Tier tier={c.tier} />
              <span className="text-muted"> · </span>
              <RecordLink r={c.record} />
              <div className="text-muted">{c.detail}</div>
            </li>
          ))}
        </ul>
      )}
      {(panel.unknown.length > 0 || panel.partial) && (
        <div className="text-[12px] text-muted">
          {panel.partial && (
            <p data-testid="impact-partial">
              {panel.partial_reason === "withheld_by_permission"
                ? "Partial: some linked records are not visible to you, so this shows only what you may read."
                : "Partial: the walk stopped at a bound, so this is a lower bound."}
            </p>
          )}
          <ul className="list-disc pl-4">
            {panel.unknown.slice(0, 5).map((u, i) => (
              <li key={i}>Unknown: {u}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
