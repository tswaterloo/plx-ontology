"use client";

import { useCallback, useEffect, useState } from "react";
import { DOC_TYPE_LABEL, refusal, shareLabel, TemplateFilledView, TemplateForm } from "@/components/templates";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { deleteTemplate, fillTemplate, getTemplates } from "@/lib/api";
import { eastern } from "@/lib/format";
import { plainError } from "@/lib/errors";
import type { Template, TemplateFilled, TemplateList } from "@/lib/types";

const NEW_BODY = "Hello {{customer_name}},\n\nYour order {{sales_order_number}} (your PO {{po_number}}) is confirmed.\n\n{{items}}\n";

/** One template: its type, who it is shared with, how often used; use it on a record, change
 * or delete it (yours only). */
function Row({ t, list, onChanged }: { t: Template; list: TemplateList; onChanged: () => void }) {
  const [mode, setMode] = useState<"view" | "edit" | "use">("view");
  const [record, setRecord] = useState("");
  const [filled, setFilled] = useState<TemplateFilled | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const use = async () => {
    setProblem(null);
    try {
      setFilled(await fillTemplate(t.id, record.trim() || null));
    } catch (err) {
      setProblem(refusal(err));
    }
  };
  const remove = async () => {
    try {
      await deleteTemplate(t.id);
      onChanged();
    } catch (err) {
      setProblem(refusal(err));
    }
  };
  const shared = t.mine ? ((t.shared_with ?? []).length ? `Shared with ${(t.shared_with ?? []).map(shareLabel).join(", ")}` : "Only you") : "Shared with you";
  return (
    <li className="space-y-2 py-3.5" data-testid="template-row">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p className="text-[14px] font-medium">{t.name}</p>
        <p className="text-[12px] text-muted">
          {DOC_TYPE_LABEL[t.doc_type]} · {shared} · {t.uses ? `used ${t.uses} time${t.uses === 1 ? "" : "s"}` : "not used yet"}
          {t.last_used_at ? `, last ${eastern(t.last_used_at)}` : ""}
        </p>
        <div className="ml-auto flex gap-1">
          <button type="button" className="h-7 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer" onClick={() => setMode(mode === "use" ? "view" : "use")} data-testid="template-use">
            Use
          </button>
          {t.mine && (
            <>
              <button type="button" className="h-7 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer" onClick={() => setMode(mode === "edit" ? "view" : "edit")}>
                Edit
              </button>
              <button type="button" className="h-7 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover cursor-pointer" onClick={remove} data-testid="template-delete">
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {mode === "view" && <p className="line-clamp-2 whitespace-pre-wrap text-[13px] leading-5 text-muted">{t.body}</p>}
      {mode === "edit" && (
        <TemplateForm
          initial={t}
          list={list}
          onSaved={() => {
            setMode("view");
            onChanged();
          }}
          onCancel={() => setMode("view")}
        />
      )}
      {mode === "use" && (
        <div className="space-y-2">
          <div className="flex gap-2">
            <input
              className="w-72 rounded-md bg-bg px-2.5 py-1.5 text-[13px] ring-1 ring-border-strong focus:outline-none focus:ring-accent"
              placeholder="Record, for example SO 5512 or PO 1234"
              value={record}
              onChange={(e) => setRecord(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && use()}
              data-testid="template-record"
            />
            <button type="button" className="h-8 rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg cursor-pointer" onClick={use}>
              Fill
            </button>
          </div>
          {filled && <TemplateFilledView filled={filled} />}
        </div>
      )}
      {problem && <p className="text-[12px] text-muted">{problem}</p>}
    </li>
  );
}

/** Templates: the documents you write often (quotes, confirmations, supplier emails, PO notes,
 * form layouts, phrasing), filled from a record when used. Yours unless you share one. */
export default function TemplatesPage() {
  const [list, setList] = useState<TemplateList | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const [adding, setAdding] = useState(false);
  const load = useCallback(() => {
    const c = new AbortController();
    setProblem(null);
    getTemplates(c.signal)
      .then(setList)
      .catch((e) => !c.signal.aborted && setProblem(e));
    return () => c.abort();
  }, []);
  useEffect(() => load(), [load]);
  const mine = (list?.templates ?? []).filter((t) => t.mine);
  const shared = (list?.templates ?? []).filter((t) => !t.mine);
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar
        heading
        title="Templates"
        actions={
          list?.available ? (
            <button type="button" className="inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] cursor-pointer" onClick={() => setAdding(true)} data-testid="template-new">
              New template
            </button>
          ) : null
        }
      />
      <div className="w-full max-w-[60rem] overflow-y-auto px-6 py-5">
        <p className="mb-4 max-w-[70ch] text-[13px] leading-5 text-muted">
          Documents you write often, with {"{{field}}"} where a record&apos;s value goes. Using one fills each field from the record you name, with where it came from; anything the records don&apos;t settle is left marked check. In the Ask box, type &ldquo;use my order confirmation template for SO 5512&rdquo;.
        </p>
        {problem ? <LoadProblem message={plainError(problem)} onRetry={load} /> : null}
        {!list && !problem && <Pending label="Reading your templates" />}
        {list && !list.available && <EmptyState title="Templates are not available right now." />}
        {list?.available && adding && (
          <section className="mb-6 rounded-md bg-bg px-4 py-3.5">
            <h2 className="mb-2 text-[15px] font-semibold">New template</h2>
            <TemplateForm
              initial={{ body: NEW_BODY, doc_type: "order_confirmation" }}
              list={list}
              onSaved={() => {
                setAdding(false);
                load();
              }}
              onCancel={() => setAdding(false)}
            />
          </section>
        )}
        {list?.available && (
          <>
            <section aria-labelledby="mine-title">
              <h2 id="mine-title" className="text-[15px] font-semibold">Yours</h2>
              {mine.length === 0 ? (
                <p className="mt-1 text-[13px] text-muted">None yet. Save one here, or with &ldquo;Save as template&rdquo; under a draft or a filled form.</p>
              ) : (
                <ul className="divide-y divide-border">{mine.map((t) => <Row key={t.id} t={t} list={list} onChanged={load} />)}</ul>
              )}
            </section>
            {shared.length > 0 && (
              <section aria-labelledby="shared-title" className="mt-8">
                <h2 id="shared-title" className="text-[15px] font-semibold">Shared with you</h2>
                <ul className="divide-y divide-border">{shared.map((t) => <Row key={t.id} t={t} list={list} onChanged={load} />)}</ul>
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
