"use client";

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { bizPage, suggest } from "@/lib/api";
import { doorKey, recordLabel, safeHref, SYSTEM_LABEL, typeLabel } from "@/lib/format";
import type { BizPage, Suggestion } from "@/lib/types";
import { MissingActions, ObjectView } from "./object-view";
import { arrange } from "./ask-box";
import { SystemCode } from "./ui/glyphs";
import { Pending } from "./ui/skeleton";

/** The record type each friendly path opens: /po/106093, /customer/DIVE11, /supplier/V06110, /ao/AO-26253A. */
export const FRIENDLY_TYPE = {
  po: "PurchaseOrder",
  customer: "Customer",
  supplier: "Supplier",
  ao: "AssemblyOrder",
} as const;
export type FriendlyKind = keyof typeof FRIENDLY_TYPE;

/** Fast ERP business-table door kind for friendly paths that have a business-table view. */
const FRIENDLY_DOOR_KIND = {
  po: "purchase_order",
  customer: "customer",
  supplier: "supplier",
} as const;
type FriendlyDoorKind = (typeof FRIENDLY_DOOR_KIND)[keyof typeof FRIENDLY_DOOR_KIND];

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

/** How long the "named like it" lookup may take before it stops and offers Retry. */
export const CHOOSER_LIMIT_MS = 15_000;

/** Shown when a friendly path finds no one record: at once, not found and search; then the
 * records of that name the reader may see, to pick from (never a page waiting on the lookup). */
function Chooser({ type, refText }: { type: string; refText: string }) {
  const [found, setFound] = useState<ReturnType<typeof namedBy> | null>(null);
  const [near, setNear] = useState(false); // found by the start of a name, not the name itself
  const [tries, setTries] = useState(0);
  useEffect(() => {
    const c = new AbortController();
    setFound(null);
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
  }, [type, refText, tries]);
  const kind = typeLabel(type).toLowerCase();
  // "PO 100040", "Customer Diversified": how the reader names what they opened
  const said = /^(?:PO|SO)\s/.test(recordLabel(type, refText)) ? recordLabel(type, refText) : `${typeLabel(type)} ${refText}`;
  const title = found === null ? said : found.length === 0 ? `No ${kind} “${refText}” that you can open` : near ? `${said}: close matches` : found.length > 1 ? `${said}: ${found.length} records` : said;
  const sub =
    found === null ? (
      `No single ${kind} opens by “${refText}”. Search for it, or pick one named like it below.`
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
        {(found === null || found.length === 0 || near) && <MissingActions />}
      </header>
      {found === null && <Pending key={tries} label={`Looking for records named “${refText}”`} limit={CHOOSER_LIMIT_MS} onRetry={() => setTries((n) => n + 1)} after={400} />}
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

/** The record a friendly path names, rendered in place with fast ERP door lookup (~100ms) and
 *  on-demand lazy sections so header, KPIs, lines, and balances appear immediately while
 *  ontology enrichment streams in progressively. */
export function FriendlyRecord({ kind }: { kind: FriendlyKind }) {
  const { ref } = useParams<{ ref: string }>();
  const text = friendlyRef(ref);
  if (!text) notFound();
  const type = FRIENDLY_TYPE[kind];
  const doorKind: FriendlyDoorKind | null = kind in FRIENDLY_DOOR_KIND ? FRIENDLY_DOOR_KIND[kind as keyof typeof FRIENDLY_DOOR_KIND] : null;
  const [page, setPage] = useState<BizPage | null>(null);
  const [doorAttempt, setDoorAttempt] = useState(0);
  const retryDoor = useCallback(() => setDoorAttempt((n) => n + 1), []);

  useEffect(() => {
    setPage(null);
    if (!doorKind) return;
    const ctl = new AbortController();
    suggest(text, ctl.signal, 10)
      .then((res) => {
        if (ctl.signal.aborted) return null;
        const matches = namedBy(res?.suggestions ?? [], type, text);
        // Never guess when multiple records share the reference (let ObjectView / Chooser disambiguate).
        if (matches.length !== 1) return null;
        const hit = matches[0];
        const key = doorKey(hit.open_url, type, hit.object_id);
        if (!key || key.kind !== doorKind) return null;
        return bizPage(key.kind, key.h ? { h: key.h } : { record: key.record }, ctl.signal);
      })
      .then(
        (p) => {
          if (!ctl.signal.aborted && p && p.kind === doorKind && Array.isArray(p.facts) && Array.isArray(p.sections)) {
            setPage(p);
          }
        },
        () => undefined,
      );
    return () => ctl.abort();
  }, [doorKind, type, text, doorAttempt]);

  return (
    <ObjectView
      key={`${type}:${text}`}
      type={type}
      id={text}
      door={page ?? undefined}
      onRetryDoor={doorKind ? retryDoor : undefined}
      lazySections
      missing={<Chooser type={type} refText={text} />}
    />
  );
}
