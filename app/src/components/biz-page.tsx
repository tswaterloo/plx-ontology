"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Toolbar } from "@/components/ui/toolbar";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { bizPage } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { formatDate, money, safeHref } from "@/lib/format";
import type { BizCell, BizKind, BizPage as Page, BizTable } from "@/lib/types";

const KINDS: readonly BizKind[] = ["customer", "chain", "supplier", "purchase_order", "sales_order"];

/** The kind a URL names, from the kinds this page shows; anything else is not a page. */
export function kindFrom(raw: string | null, allowed: readonly BizKind[]): BizKind | null {
  return raw && (allowed as readonly string[]).includes(raw) && (KINDS as readonly string[]).includes(raw) ? (raw as BizKind) : null;
}

function Cell({ c, numeric }: { c: BizCell; numeric?: boolean }) {
  if (c.hidden) return <span className="text-muted" title="You don't have access to this value">Restricted</span>;
  const text = c.money ? money(c.money) : c.day ? formatDate(c.day, { short: true }) : c.number ?? c.text ?? "";
  const href = safeHref(c.href);
  const body = href ? (
    <Link href={href} className="text-accent hover:underline">
      {text}
    </Link>
  ) : (
    text
  );
  return <span className={numeric ? "tabular-nums" : undefined}>{body}</span>;
}

function isNumeric(rows: BizCell[][], i: number): boolean {
  return rows.some((r) => r[i]?.money || r[i]?.number);
}

function Section({ s }: { s: BizTable }) {
  const numeric = s.columns.map((_, i) => isNumeric(s.rows, i));
  return (
    <section className="mt-7" aria-labelledby={`biz-${s.key}`} data-testid={`biz-section-${s.key}`}>
      <h2 id={`biz-${s.key}`} className="text-[14px] font-medium text-fg">
        {s.title}
      </h2>
      {s.rows.length > 0 && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[12px] text-muted">
                {s.columns.map((c, i) => (
                  <th key={c} scope="col" className={`py-1.5 pr-4 font-normal ${numeric[i] ? "text-right" : ""}`}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {s.rows.map((r, j) => (
                <tr key={j} className="border-b border-border/60">
                  {r.map((c, i) => (
                    <td key={i} className={`py-1.5 pr-4 align-top ${numeric[i] ? "text-right" : ""}`}>
                      <Cell c={c} numeric={numeric[i]} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {s.note && <p className="mt-2 text-[12px] text-muted">{s.note}</p>}
    </section>
  );
}

function problemText(e: unknown): string {
  if (e instanceof ApiError && e.detail && (e.status === 404 || e.status === 503)) return e.detail;
  return plainError(e);
}

/** A business record page: the header facts, then each section, then one Sources line.
 * While it reads: a skeleton that ends (a limit, then Retry); never an empty verdict. */
export function BizRecordPage({ allowed, fallbackTitle }: { allowed: readonly BizKind[]; fallbackTitle: string }) {
  const params = useSearchParams();
  const kind = kindFrom(params.get("kind"), allowed);
  const h = params.get("h") ?? "";
  const chain = params.get("chain") ?? "";
  const [page, setPage] = useState<Page | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const retry = useCallback(() => setRound((n) => n + 1), []);

  useEffect(() => {
    setPage(null);
    setProblem(null);
    if (!kind) return;
    const ctl = new AbortController();
    bizPage(kind, { h, chain }, ctl.signal).then(setPage, (e) => !ctl.signal.aborted && setProblem(e));
    return () => ctl.abort();
  }, [kind, h, chain, round]);

  const title = page?.title ?? fallbackTitle;
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar heading title={title} />
      <div className="w-full max-w-[960px] overflow-y-auto px-6 py-5">
        {!kind ? (
          <p className="text-[13px] text-muted">This link doesn&apos;t name a record. Find it under Records.</p>
        ) : problem ? (
          <LoadProblem message={problemText(problem)} busy={problem instanceof ApiError && problem.code === "busy"} onRetry={retry} />
        ) : !page ? (
          <Pending label={`Reading ${fallbackTitle.toLowerCase()}`} page onRetry={retry} />
        ) : (
          <article data-testid="biz-page">
            {page.subtitle && <p className="text-[12px] text-muted">{page.subtitle}</p>}
            {page.facts.length > 0 && (
              <dl className="mt-4 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1.5 text-[13px] sm:grid-cols-[max-content_1fr_max-content_1fr]">
                {page.facts.map((f) => (
                  <div key={f.label} className="contents">
                    <dt className="text-muted">{f.label}</dt>
                    <dd>
                      <Cell c={f.value} />
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            {page.sections.map((s) => (
              <Section key={s.key} s={s} />
            ))}
            <p className="mt-8 text-[12px] text-muted" data-testid="biz-sources">
              {page.sources}
            </p>
          </article>
        )}
      </div>
    </div>
  );
}
