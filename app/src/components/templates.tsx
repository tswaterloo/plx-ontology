"use client";

// Saved templates: a template filled from a record (every placeholder with the record field
// it came from, or marked check with why), "Save as template" from a draft, a filled form or
// a document, and "Use template" where a document is started. Templates are the person's
// own unless they share one; nothing here sends anything.

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/errors";
import { fillTemplate, getTemplates, proposeTemplate, saveTemplate, suggestTemplates } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { Template, TemplateDocType, TemplateFilled, TemplateList, TemplateSuggestion } from "@/lib/types";
import { Button } from "./ui/button";

export const DOC_TYPE_LABEL: Record<TemplateDocType, string> = {
  quote: "Quote",
  order_confirmation: "Order confirmation",
  supplier_email: "Supplier email",
  customer_email: "Customer email",
  po_note: "PO note",
  form: "Form layout",
  phrasing: "House phrasing",
  price_list: "Price list",
  proposal: "Proposal",
  spec_sheet: "Spec sheet",
  coa: "Certificate of analysis",
  letter: "Letter",
  other: "Other",
};

/** The server's own words for a refusal (they say what to change), else the plain error. */
export function refusal(err: unknown): string {
  return err instanceof ApiError && err.detail && err.status < 500 ? err.detail : plainError(err);
}

/** A share entry in words: "role:purchasing" -> "Purchasing", "person:a@b" -> "a@b". */
export function shareLabel(entry: string): string {
  const [kind, ...rest] = entry.split(":");
  const value = rest.join(":");
  if (kind === "role") return value.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
  return value;
}

/** Parse the share box: addresses as people, anything else as a role name. */
export function sharesOf(text: string): string[] {
  return text
    .split(/[,;\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => (/^(role|person):/.test(s) ? s : s.includes("@") ? `person:${s.toLowerCase()}` : `role:${s.toLowerCase().replace(/\s+/g, "_")}`));
}

/** A filled template: the text to copy, then each placeholder and its source or why to check it. */
export function TemplateFilledView({ filled }: { filled: TemplateFilled }) {
  const [copied, setCopied] = useState(false);
  const fields = filled.fields ?? [];
  const copy = () => {
    void navigator.clipboard?.writeText(filled.text).then(() => setCopied(true));
  };
  return (
    <section className="space-y-2 border-l-2 border-accent pl-3" data-testid="template-filled">
      <p className="text-[13px] leading-5">
        <span className="font-medium">{filled.name}</span>
        {filled.record && (
          <>
            {" "}for{" "}
            <a className="text-accent hover:underline" href={filled.record.open_url ?? undefined}>
              {filled.record.label}
            </a>
          </>
        )}
        <span className="text-muted">
          {" "}
          · {fields.length - (filled.checks ?? 0)} of {fields.length} filled
          {(filled.checks ?? 0) > 0 && `, ${filled.checks} to check`}
        </span>
      </p>
      <pre className="whitespace-pre-wrap break-words rounded-md bg-surface px-3 py-2 font-sans text-[13px] leading-[21px]" data-testid="template-text">
        {filled.text}
      </pre>
      {filled.style_notes && <p className="text-[12px] leading-4 text-muted">Style: {filled.style_notes}</p>}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[28rem] text-[13px] leading-5" data-testid="template-fields">
          <thead className="text-left text-[12px] text-muted">
            <tr>
              <th className="pr-4 font-normal">Placeholder</th>
              <th className="pr-4 font-normal">Value</th>
              <th className="font-normal">From</th>
            </tr>
          </thead>
          <tbody>
            {fields.map((f) => (
              <tr key={f.name} data-status={f.status} className="align-top">
                <td className="pr-4">{f.label}</td>
                <td className={f.status === "filled" ? "pr-4 font-medium whitespace-pre-wrap" : "pr-4 text-muted"}>{f.status === "filled" ? f.value : "Check"}</td>
                <td className="text-[12px]">
                  {f.status === "filled" && f.citation ? (
                    <a className="text-accent hover:underline" href={f.citation.open_url ?? undefined}>
                      {f.citation.record} · {f.citation.field}
                    </a>
                  ) : (
                    <span className="text-muted">{f.reason}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(filled.notes ?? []).map((n, i) => (
        <p key={i} className="text-[12px] leading-4 text-muted">
          {n}
        </p>
      ))}
      <div className="flex items-center gap-2">
        <Button size="sm" variant="secondary" onClick={copy} data-testid="template-copy">
          {copied ? "Copied" : "Copy text"}
        </Button>
        <span className="text-[12px] text-muted">Read it through and replace every check mark in brackets before you use it.</span>
      </div>
    </section>
  );
}

/** The save form: name, type, body (placeholders proposed from the record), style notes, who
 * else may use it. Used for a new template, a change to one of yours, and "Save as template". */
export function TemplateForm({
  initial,
  list,
  onSaved,
  onCancel,
}: {
  initial: Partial<Template> & { body: string };
  list: TemplateList | null;
  onSaved: (t: Template) => void;
  onCancel?: () => void;
}) {
  const [name, setName] = useState(initial.name ?? "");
  const [docType, setDocType] = useState<TemplateDocType>(initial.doc_type ?? "other");
  const [body, setBody] = useState(initial.body);
  const [style, setStyle] = useState(initial.style_notes ?? "");
  const [share, setShare] = useState((initial.shared_with ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setProblem(null);
    try {
      const saved = await saveTemplate(
        { name: name.trim(), doc_type: docType, body, style_notes: style, shared_with: sharesOf(share), source_ref: initial.source_ref ?? null },
        initial.id,
      );
      onSaved(saved);
    } catch (err) {
      setProblem(refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const field = "w-full rounded-md bg-bg px-2.5 py-1.5 text-[13px] ring-1 ring-border-strong focus:outline-none focus:ring-accent";
  return (
    <div className="space-y-2.5" data-testid="template-form">
      <div className="grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_14rem]">
        <label className="space-y-1 text-[12px] text-muted">
          Name
          <input className={field} value={name} onChange={(e) => setName(e.target.value)} maxLength={120} data-testid="template-name" />
        </label>
        <label className="space-y-1 text-[12px] text-muted">
          Type
          <select className={field} value={docType} onChange={(e) => setDocType(e.target.value as TemplateDocType)} data-testid="template-type">
            {Object.entries(DOC_TYPE_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="block space-y-1 text-[12px] text-muted">
        Text, with {"{{field}}"} where a record&apos;s value goes
        <textarea className={`${field} min-h-40 font-mono text-[12px] leading-5`} value={body} onChange={(e) => setBody(e.target.value)} maxLength={8000} data-testid="template-body" />
      </label>
      {list && (list.fields ?? []).length > 0 && (
        <p className="text-[12px] leading-5 text-muted" data-testid="template-field-list">
          Fields:{" "}
          {(list.fields ?? []).map((f, i) => (
            <span key={f.name}>
              {i > 0 && ", "}
              <button type="button" className="text-accent hover:underline cursor-pointer" onClick={() => setBody((b) => `${b}{{${f.name}}}`)}>
                {f.label}
              </button>
            </span>
          ))}
        </p>
      )}
      <label className="block space-y-1 text-[12px] text-muted">
        Style notes (tone, sign-off, layout)
        <input className={field} value={style} onChange={(e) => setStyle(e.target.value)} maxLength={2000} />
      </label>
      <label className="block space-y-1 text-[12px] text-muted">
        Share with (colleagues&apos; addresses or roles, comma separated; empty keeps it to you)
        <input className={field} value={share} onChange={(e) => setShare(e.target.value)} placeholder={(list?.share_roles ?? []).slice(0, 3).join(", ")} data-testid="template-share" />
      </label>
      <p className="text-[12px] leading-4 text-muted">Prices, amounts and pay never go in a template: type them in when you use it.</p>
      {problem && (
        <p role="alert" className="rounded-lg bg-late-soft px-3 py-2 text-[13px]" data-testid="template-problem">
          {problem}
        </p>
      )}
      <div className="flex gap-2">
        <Button size="sm" onClick={save} disabled={busy || !name.trim() || !body.trim()} data-testid="template-save">
          {initial.id ? "Save changes" : "Save template"}
        </Button>
        {onCancel && (
          <Button size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
      </div>
    </div>
  );
}

/** "Save as template" beside a draft, a filled form or a made document: the text with the
 * record's values turned into placeholders, for the person to check and save. */
export function SaveAsTemplate({ text, record, docType, sourceRef }: { text: string; record?: string | null; docType: TemplateDocType; sourceRef?: string | null }) {
  const [open, setOpen] = useState(false);
  const [initial, setInitial] = useState<{ body: string } | null>(null);
  const [list, setList] = useState<TemplateList | null>(null);
  const [saved, setSaved] = useState<Template | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const start = async () => {
    setOpen(true);
    setProblem(null);
    try {
      const [proposal, got] = await Promise.all([proposeTemplate(text.slice(0, 8000), record), getTemplates()]);
      setList(got);
      setInitial({ body: proposal.body });
    } catch (err) {
      setProblem(refusal(err));
      setInitial({ body: text.slice(0, 8000) });
    }
  };
  if (saved) return <p className="text-[12px] text-muted" data-testid="template-saved">Saved as template &ldquo;{saved.name}&rdquo;.</p>;
  if (!open)
    return (
      <Button size="sm" variant="ghost" className="h-7 px-2 text-[12px]" onClick={start} data-testid="save-as-template">
        Save as template
      </Button>
    );
  return (
    <div className="rounded-md bg-surface px-3 py-2.5">
      {problem && <p className="mb-2 text-[12px] text-muted">{problem}</p>}
      {initial ? (
        <TemplateForm initial={{ ...initial, doc_type: docType, source_ref: sourceRef ?? null }} list={list} onSaved={setSaved} onCancel={() => setOpen(false)} />
      ) : (
        <p className="text-[12px] text-muted">Reading the record</p>
      )}
    </div>
  );
}

/** "Use template" where a document of a type is started: the templates offered for it (saved,
 * shared, then the layout learned from what you sent), and one filled from the record. */
export function UseTemplate({ docTypes, record }: { docTypes: TemplateDocType[]; record?: string | null }) {
  const [offers, setOffers] = useState<TemplateSuggestion[] | null>(null);
  const [filled, setFilled] = useState<TemplateFilled | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const key = docTypes.join(",");
  useEffect(() => {
    const c = new AbortController();
    Promise.all(key.split(",").map((t) => suggestTemplates(t as TemplateDocType, c.signal)))
      .then((all) => setOffers(all.flatMap((s) => s.suggestions ?? [])))
      .catch(() => !c.signal.aborted && setOffers([]));
    return () => c.abort();
  }, [key]);
  if (!offers || offers.length === 0) return null;
  const use = async (t: Template) => {
    setProblem(null);
    try {
      setFilled(await fillTemplate(t.id, record));
    } catch (err) {
      setProblem(refusal(err));
    }
  };
  return (
    <div className="space-y-2" data-testid="use-template">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] leading-5 text-muted">
        <span>Your templates for this:</span>
        {offers.map((s, i) =>
          s.template ? (
            <Button key={`t${s.template.id}`} size="sm" variant="outline" className="h-7 px-2 text-[12px] font-normal" title={s.detail} onClick={() => use(s.template as Template)} data-testid="template-offer">
              {s.label}
            </Button>
          ) : s.ask ? (
            <span key={`l${i}`} data-testid="template-learned" title={s.detail}>
              {s.label} ({s.detail}): type &ldquo;{s.ask.trim()}&rdquo; and the record in the Ask box
            </span>
          ) : null,
        )}
      </p>
      {problem && <p className="text-[12px] text-muted">{problem}</p>}
      {filled && <TemplateFilledView filled={filled} />}
    </div>
  );
}
