"use client";

import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { suggest } from "@/lib/api";
import { recordLabel, safeHref, typeLabel } from "@/lib/format";
import type { Suggestion } from "@/lib/types";
import { ObjectView } from "./object-view";
import { arrange } from "./ask-box";
import { Card } from "./ui/card";
import { PageHeader } from "./ui/page-header";

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
  // "PO PO100040", "PO100040" and "100040" name one purchase order
  const said = (t: string, l: string) => fold(recordLabel(t, l));
  const want = said(type, ref);
  return arrange(list, ref)
    .filter((s) => fam(s.type) === fam(type))
    .filter((s) => said(s.type, s.label) === want || (s.facts ?? "").split(" · ").some((f) => fold(f) === fold(ref) || said(type, f) === want));
}

/** The suggestions of `type` whose name starts with, or has a word starting with, `ref`: "Did you mean" when none is named by it. */
export function startingWith(list: Suggestion[], type: string, ref: string) {
  const fam = (t: string) => t.replace(/[_\s-]/g, "").toLowerCase().replace(/^vendor$/, "supplier");
  const want = fold(ref);
  if (!want) return [];
  return arrange(list, ref)
    .filter((s) => fam(s.type) === fam(type))
    .filter((s) => ` ${fold(s.label)}`.includes(` ${want}`));
}

/** Shown when a friendly path finds no one record: the records of that name the reader may see, to pick from. */
function Chooser({ type, refText }: { type: string; refText: string }) {
  const [found, setFound] = useState<ReturnType<typeof namedBy> | null>(null);
  const [near, setNear] = useState(false); // found by the start of a name, not the name itself
  useEffect(() => {
    const c = new AbortController();
    suggest(refText, c.signal)
      .then((r) => {
        const named = namedBy(r.suggestions ?? [], type, refText);
        setNear(named.length === 0);
        setFound(named.length ? named : startingWith(r.suggestions ?? [], type, refText));
      })
      .catch(() => {
        if (!c.signal.aborted) setFound([]);
      });
    return () => c.abort();
  }, [type, refText]);
  const kind = typeLabel(type).toLowerCase();
  // "PO 100040", "Customer Diversified": how the reader names what they opened
  const said = /^(?:PO|SO)\s/.test(recordLabel(type, refText)) ? recordLabel(type, refText) : `${typeLabel(type)} ${refText}`;
  return (
    <div className="space-y-3" data-testid="friendly-chooser">
      <PageHeader
        title={found === null ? said : found.length === 0 ? `No ${kind} “${refText}” that you can open` : near ? `${said}: close matches` : found.length > 1 ? `${said}: ${found.length} records` : said}
        sub={
          found === null ? (
            <span role="status">Looking for {kind} “{refText}”…</span>
          ) : found.length === 0 ? (
            "Check the number or name, or search above."
          ) : near ? (
            `No ${kind} is named exactly “${refText}”. These names start with it.`
          ) : found.length > 1 ? (
            "Pick one. Records that share a name or number are kept apart."
          ) : null
        }
      />
      {found && found.length > 0 && (
        <Card className="border-t-0">
          <ul className="divide-y divide-border">
            {found.map((s) => (
              <li key={s.object_id}>
                <Link href={safeHref(s.open_url) ?? "/"} className="group flex items-baseline gap-4 py-2 hover:bg-surface">
                  <span className="w-40 shrink-0 truncate text-row text-accent group-hover:underline">{recordLabel(s.type, s.label)}</span>
                  {s.facts ? <span className="min-w-0 flex-1 truncate text-row">{s.facts}</span> : <span className="flex-1" />}
                  {s.systems.length ? <span className="shrink-0 text-meta text-muted">{s.systems.join(", ")}</span> : null}
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
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
