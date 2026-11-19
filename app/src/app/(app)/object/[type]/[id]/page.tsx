"use client";

import { notFound, useParams } from "next/navigation";
import { BizOrObject } from "@/components/biz-page";
import { ObjectView } from "@/components/object-view";
import { isObjectType } from "@/lib/api";
import { doorKey, typeLabel } from "@/lib/format";

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

export default function ObjectPageRoute() {
  const { type, id } = useParams<{ type: string; id: string }>();
  // A malformed type fails closed: no request is made.
  if (!isObjectType(type) || !id) notFound();
  const record = safeDecode(id);
  const own = <ObjectView type={type} id={record} />;
  // a PO, customer order, customer or supplier opens its page over the business tables
  // when the server has one for it (the pages door); its own page otherwise
  const door = doorKey(null, type, record);
  return door?.record ? <BizOrObject key={record} kind={door.kind} record={door.record} title={typeLabel(type)} fallback={own} /> : own;
}
