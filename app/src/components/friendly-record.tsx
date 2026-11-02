"use client";

import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { suggest } from "@/lib/api";
import { safeHref, typeLabel } from "@/lib/format";
import type { Suggestion } from "@/lib/types";
import { ObjectView } from "./object-view";
import { arrange, sharedName } from "./ask-box";
import { Card, CardBody } from "./ui/card";

/** The record type each friendly path opens: /po/106093, /customer/DIVE11, /supplier/V06110, /ao/AO-26253A. */
export const FRIENDLY_TYPE = {
  po: "PurchaseOrder",
  customer: "Customer",
  supplier: "Supplier",
  ao: "AssemblyOrder",
} as const;
export type FriendlyKind = keyof typeof FRIENDLY_TYPE;

/**
 * A friendly path's reference, or null: 1 to 100 characters with no ":" (a whole object
 * id is never taken from a friendly path), "/", "\" or control character.
 */
export function friendlyRef(raw: string | undefined): string | null {
  if (!raw) return null;
  let ref: string;
  try {
    ref = decodeURIComponent(raw).trim();
  } catch {
    return null;
  }
  // eslint-disable-next-line no-control-regex
  if (!ref || ref.length > 100 || /[:/\\\u0000-\u001f\u007f]/.test(ref)) return null;
  return ref;
}

const fold = (s: string) => s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

/** The suggestions of `type` that `ref` names: by name, or by a code they carry. */
export function namedBy(list: Suggestion[], type: string, ref: string) {
  const fam = (t: string) => t.replace(/[_\s-]/g, "").toLowerCase().replace(/^vendor$/, "supplier");
  const want = fold(ref);
  return arrange(list, ref)
    .filter((s) => fam(s.type) === fam(type))
    .filter((s) => fold(s.label) === want || (s.facts ?? "").split(" · ").some((f) => fold(f) === want));
}

/** Shown when a friendly path finds no one record: the records of that name the reader may see, to pick from. */
function Chooser({ type, refText }: { type: string; refText: string }) {
  const [found, setFound] = useState<ReturnType<typeof namedBy> | null>(null);
  useEffect(() => {
    const c = new AbortController();
    suggest(refText, c.signal)
      .then((r) => setFound(namedBy(r.suggestions ?? [], type, refText)))
      .catch(() => {
        if (!c.signal.aborted) setFound([]);
      });
    return () => c.abort();
  }, [type, refText]);
  const kind = typeLabel(type).toLowerCase();
  return (
    <Card data-testid="friendly-chooser">
      <CardBody>
        {found === null ? (
          <p className="text-meta text-muted" role="status">Looking for {kind} “{refText}”…</p>
        ) : found.length === 0 ? (
          <p className="text-answer">No {kind} “{refText}” that you can open.</p>
        ) : (
          <>
            <p className="text-answer">{found.length > 1 ? `${sharedName(found.length, type)}.` : `One ${kind} is named “${refText}”.`}</p>
            <ul className="mt-3 divide-y divide-border">
              {found.map((s) => (
                <li key={s.object_id} className="py-2">
                  <Link href={safeHref(s.open_url) ?? "/"} className="text-accent hover:underline">
                    {s.label} →
                  </Link>
                  {s.facts || s.systems.length ? <span className="text-meta text-muted"> {[s.facts, s.systems.join(" and ")].filter(Boolean).join(" · ")}</span> : null}
                </li>
              ))}
            </ul>
          </>
        )}
      </CardBody>
    </Card>
  );
}

/** The record a friendly path names, rendered in place (the API resolves the reference within the type). */
export function FriendlyRecord({ kind }: { kind: FriendlyKind }) {
  const { ref } = useParams<{ ref: string }>();
  const text = friendlyRef(ref);
  if (!text) notFound();
  const type = FRIENDLY_TYPE[kind];
  return <ObjectView type={type} id={text} missing={<Chooser type={type} refText={text} />} />;
}
