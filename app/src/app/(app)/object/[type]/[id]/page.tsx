"use client";

import { notFound, useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ObjectView } from "@/components/object-view";
import { bizPage, isObjectType } from "@/lib/api";
import { doorKey } from "@/lib/format";
import type { BizPage } from "@/lib/types";

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/**
 * Every record kind opens the one record page (Follow, Save, history, landed cost, margin,
 * sheets and mail). An order, customer or supplier also reads its business tables' page
 * (about 100 ms) beside it, for the lines, invoices and what is owed, each number linking
 * to its record; a record without one, or one it cannot read, shows its own page alone.
 */
/** An item's, a batch's or a supplier invoice's page over the business tables (stock, batches, QC readings, what is owed). */
type ItemKind = "product" | "batch" | "supplier_invoice";
const ITEM_KINDS: Readonly<Record<string, ItemKind>> = { Product: "product", Batch: "batch", RelSageApOpenDocument: "supplier_invoice" };
function itemKey(type: string, record: string): { kind: ItemKind; record: string } | null {
  const kind = ITEM_KINDS[type];
  return kind && record.startsWith(`${type}:`) && record.length <= 700 ? { kind, record } : null;
}

export default function ObjectPageRoute() {
  const { type, id } = useParams<{ type: string; id: string }>();
  // A malformed type fails closed: no request is made.
  if (!isObjectType(type) || !id) notFound();
  const record = safeDecode(id);
  const door = doorKey(null, type, record) ?? itemKey(type, record);
  const [page, setPage] = useState<BizPage | null>(null);
  useEffect(() => {
    setPage(null);
    if (!door?.record) return;
    const ctl = new AbortController();
    bizPage(door.kind, { record: door.record }, ctl.signal).then(setPage, () => undefined);
    return () => ctl.abort();
  }, [door?.kind, door?.record]);
  return <ObjectView key={record} type={type} id={record} door={page} />;
}
