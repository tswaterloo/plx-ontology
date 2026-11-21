"use client";

import { notFound, redirect, useParams } from "next/navigation";
import { isObjectType } from "@/lib/api";
import { objectHref } from "@/lib/format";

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

/** The address of a record when given as `/object/<id>` alone: the id's own prefix
 * (`SalesOrder:portal...`) is its type, so it opens the record's page. */
export default function ObjectWithoutTypeRoute(): React.ReactNode {
  const { type } = useParams<{ type: string }>();
  const record = safeDecode(type ?? "");
  const kind = record.includes(":") ? record.slice(0, record.indexOf(":")) : "";
  if (!kind || !isObjectType(kind) || record.length > 700) notFound();
  return redirect(objectHref(kind, record));
}
