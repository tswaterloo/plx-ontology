"use client";

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { suggest } from "@/lib/api";
import { recordLabel, safeHref, SYSTEM_LABEL, typeLabel } from "@/lib/format";
import type { Suggestion } from "@/lib/types";
import { MissingActions, ObjectView } from "./object-view";
import { arrange, type Arranged } from "./ask-box";
import { SystemCode } from "./ui/glyphs";

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

/** Who a suggestion is about, for its tile: a party's own name, else the name its facts give ("DSM (V06110)"). */
function partyOf(s: Arranged): string | null {
  if (/^(Customer|Supplier|Vendor)$/.test(s.type)) return s.label;
  const named = (s.facts ?? "").split(" · ").find((f) => /[A-Za-z]{2,}\s+[A-Za-z(]/.test(f));
  return named ? named.replace(/\s*\(.*\)\s*$/, "") : null;
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
  const title = found === null ? said : found.length === 0 ? `No ${kind} “${refText}” that you can open` : near ? `${said}: close matches` : found.length > 1 ? `${said}: ${found.length} records` : said;
  const sub =
    found === null ? (
      <span role="status" data-pending="">
        Looking for {kind} “{refText}”…
      </span>
    ) : found.length === 0 ? (
      "Check the number or name, or search for it."
    ) : near ? (
      `None is named exactly “${refText}”.`
    ) : null;
  return (
    <div className="max-w-[880px] space-y-6" data-testid="friendly-chooser">
      <header>
        <h1 className="text-[24px] leading-[30px] font-semibold tracking-[-0.025em] break-words">{title}</h1>
        {sub && <p className="mt-1 text-[13px] leading-5 text-muted">{sub}</p>}
        {found && found.length === 0 && <MissingActions />}
      </header>
      {found && found.length > 0 && (
        <ul className="-mx-3">
          {found.map((s) => (
            <li key={s.object_id}>
              <Link href={safeHref(s.open_url) ?? "/"} className="flex items-center gap-3 rounded-[3px] px-3 py-2.5 hover:bg-hover">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] leading-[18px] font-semibold">{recordLabel(s.type, s.label)}</span>
                  <span className="block truncate text-[12px] leading-4 text-muted">{s.facts || typeLabel(s.type)}</span>
                </span>
                {s.systems.length ? (
                  <span className="flex shrink-0 items-center gap-2 text-[12px] text-muted">
                    {s.systems.map((x) => (
                      <span key={x} className="inline-flex items-center gap-1.5">
                        <SystemCode system={x} size={16} />
                        <span className="max-sm:sr-only">{SYSTEM_LABEL[x as keyof typeof SYSTEM_LABEL] ?? x}</span>
                      </span>
                    ))}
                  </span>
                ) : null}
                <ArrowUpRight className="size-3.5 shrink-0 text-faint" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
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
