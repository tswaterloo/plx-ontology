"use client";

import { notFound, useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ObjectView } from "@/components/object-view";
import { bizPage, isObjectType } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
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
 * sheets and mail). An order, customer, supplier, product, batch, or invoice reads its fast
 * business-table page (~100 ms) beside the ontology read so header, KPIs, lines, and open
 * balances render immediately while ontology enrichment loads progressively.
 */
type ItemKind = "product" | "batch" | "supplier_invoice";
const ITEM_KINDS: Readonly<Record<string, ItemKind>> = {
  Product: "product",
  Batch: "batch",
  SupplierInvoice: "supplier_invoice",
  PurchaseInvoice: "supplier_invoice",
  RelSageApOpenDocument: "supplier_invoice",
};
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
  const [doorError, setDoorError] = useState<string | null>(null);
  const [doorAttempt, setDoorAttempt] = useState(0);
  const retryDoor = useCallback(() => setDoorAttempt((n) => n + 1), []);

  useEffect(() => {
    setPage(null);
    setDoorError(null);
    if (!door?.record) return;
    const ctl = new AbortController();
    bizPage(door.kind, { record: door.record }, ctl.signal).then(
      (p) => {
        if (!ctl.signal.aborted) {
          setPage(p);
          setDoorError(null);
        }
      },
      (err) => {
        if (ctl.signal.aborted) return;
        // 404 simply means no business table page exists for this record kind
        if (err instanceof ApiError && err.status === 404) return;
        setDoorError(plainError(err));
      },
    );
    return () => ctl.abort();
  }, [door?.kind, door?.record, doorAttempt]);

  return (
    <ObjectView
      key={record}
      type={type}
      id={record}
      door={page}
      doorError={doorError}
      onRetryDoor={door ? retryDoor : undefined}
      lazySections
    />
  );
}
