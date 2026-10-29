"use client";

import { notFound, useParams } from "next/navigation";
import { ObjectView } from "@/components/object-view";
import { isObjectType } from "@/lib/api";

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
  return <ObjectView type={type} id={safeDecode(id)} />;
}
