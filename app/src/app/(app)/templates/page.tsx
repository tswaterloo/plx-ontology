"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { DOC_TYPE_LABEL, refusal, shareLabel, TemplateFilledView, TemplateForm } from "@/components/templates";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { useRead } from "@/lib/use-read";
import { StoredAge } from "@/components/ui/stored-age";
import { deleteTemplate, fillTemplate, getAnswerBank, getLearnedTemplates, getTemplates } from "@/lib/api";
import { OneList } from "@/components/record/document-lists";
import { eastern } from "@/lib/format";
import { plainError } from "@/lib/errors";
import type { DocList, LearnedTemplate, LearnedTemplates, Template, TemplateFilled, TemplateList } from "@/lib/types";

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

/** One kind learned from your sent documents: made in Ask for the record you name, the way
 * "make a quote in my usual template for Harbor" asks for it. */
function LearnedRow({ t }: { t: LearnedTemplate }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState("");
  const make = () => {
    const forWhat = record.trim();
    if (forWhat) router.push(`/ask?q=${encodeURIComponent(t.ask + forWhat)}`);
  };
  return (
    <li className="space-y-2 py-3.5" data-testid="learned-row">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p className="text-[14px] font-medium">{t.label}</p>
        <p className="text-[12px] text-muted">
          From {t.documents} of your document{t.documents === 1 ? "" : "s"}
          {t.last_sent ? `, latest ${t.last_sent}` : ""}
        </p>
        <div className="ml-auto">
          <button type="button" className="h-7 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer" onClick={() => setOpen(!open)} data-testid="learned-use">
            Make one
          </button>
        </div>
      </div>
      {open && (
        <div className="flex gap-2">
          <input
            className="w-72 rounded-md bg-bg px-2.5 py-1.5 text-[13px] ring-1 ring-border-strong focus:outline-none focus:ring-accent"
            placeholder="For, for example Harbor Naturals or SO 5512"
            value={record}
            onChange={(e) => setRecord(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && make()}
            data-testid="learned-record"
          />
          <button type="button" className="h-8 rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg cursor-pointer disabled:opacity-50" onClick={make} disabled={!record.trim()}>
            Make in Ask
          </button>
        </div>
      )}
    </li>
  );
}

/** The kinds learned from what you sent, re-learned every night. Nothing shows until some
 * are learned; a read that fails leaves the rest of the page as it is. */
function Learned() {
  const { data: learned } = useRead<LearnedTemplates>("/api/templates/learned", getLearnedTemplates);
  const rows = learned?.status === "read" ? learned.templates ?? [] : [];
  if (rows.length === 0) return null;
  return (
    <section aria-labelledby="learned-title" className="mt-8" data-testid="learned">
      <h2 id="learned-title" className="text-[15px] font-semibold">Learned from your documents</h2>
      <p className="mt-1 max-w-[70ch] text-[13px] leading-5 text-muted">
        Layouts taken from the quotes, letters and sheets you sent{learned?.learned_at ? `, last read ${eastern(learned.learned_at)}` : ""}. Each is filled from what you can open, with every changed part shown beside its source.
      </p>
      <ul className="divide-y divide-border">{rows.map((t) => <LearnedRow key={t.kind} t={t} />)}</ul>
    </section>
  );
}

/** Templates: the documents you write often (quotes, confirmations, supplier emails, PO notes,
 * form layouts, phrasing), filled from a record when used. Yours unless you share one. */
// Past questionnaire answers any customer may get, found in the questionnaires we
// answered; a question typed here ranks them. An answer given to one customer only is on
// that customer's page.
function AnswerBank() {
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  const { data: bank, error: problem, retry } = useRead<DocList>(`/api/templates/answer-bank${asked ? `?question=${encodeURIComponent(asked)}` : ""}`, (s) => getAnswerBank(asked, s));
  const rows = bank?.rows ?? [];
  return (
    <section aria-labelledby="answers-title" className="mt-8" data-testid="answer-bank">
      <h2 id="answers-title" className="text-[15px] font-semibold">Questionnaire answers</h2>
      <p className="mt-1 max-w-[70ch] text-[13px] leading-5 text-muted">
        Answers we gave in past questionnaires that any customer may get, each with the questionnaire it came from. One given to a single customer is on that customer&apos;s page.
      </p>
      <form
        className="mt-3 flex max-w-[36rem] gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setAsked(question.trim());
        }}
      >
        <input
          type="search"
          value={question}
          maxLength={700}
          onChange={(e) => {
            setQuestion(e.target.value);
            if (!e.target.value.trim()) setAsked("");
          }}
          placeholder="A question from a new questionnaire"
          aria-label="Find answers to a question"
          className="h-8 min-w-0 flex-1 rounded-md border border-border bg-bg px-2.5 text-[13px]"
        />
        <button type="submit" className="inline-flex h-8 items-center rounded-lg bg-[color-mix(in_srgb,var(--fg)_6%,transparent)] px-3 text-[13px] font-medium hover:bg-[color-mix(in_srgb,var(--fg)_10%,transparent)] cursor-pointer">
          Find
        </button>
      </form>
      <div className="mt-3">
        {problem ? <LoadProblem message={plainError(problem)} onRetry={retry} /> : null}
        {!bank && !problem && <Pending label="Reading past questionnaires" />}
        {bank?.error && <p className="text-[13px] text-muted">{bank.error}</p>}
        {bank && !bank.error && rows.length === 0 && (
          <p className="text-[13px] text-muted" data-testid="answer-bank-empty">
            {asked ? `No past answer shares a word with “${asked}”.` : "No questionnaire we answered is on record for you."}
            {bank.withheld > 0 ? ` ${bank.withheld} left out: they cite records you can't open.` : ""}
          </p>
        )}
        {bank && !bank.error && rows.length > 0 && <OneList list={{ ...bank, title: asked ? `Closest to “${asked}”` : "Answers any customer may get" }} />}
      </div>
    </section>
  );
}

export default function TemplatesPage() {
  const { data: list, error: problem, retry: load, at } = useRead<TemplateList>("/api/templates", (s) => getTemplates(s));
  const [adding, setAdding] = useState(false);
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
          Documents you write often, with blanks where a record&apos;s details go. Using one fills each blank from the record you name and shows where it came from; anything the records don&apos;t settle is marked to check. In the Ask box, type &ldquo;use my order confirmation template for SO 5512&rdquo;, or &ldquo;save a template for supplier follow-ups&rdquo; to make one from your words or from a document you name.
        </p>
        <StoredAge at={at} className="mb-3" />
        {problem ? <LoadProblem message={plainError(problem)} onRetry={load} /> : null}
        {!list && !problem && <Pending label="Reading your templates" />}
        {list && !list.available && <LoadProblem message="Your templates couldn't be read just now." onRetry={load} />}
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
                <p className="mt-1 text-[13px] text-muted">None yet. Save one here, ask for one in the Ask box, or use &ldquo;Save as template&rdquo; under a draft or a filled form.</p>
              ) : (
                <ul className="divide-y divide-border">{mine.map((t) => <Row key={t.id} t={t} list={list} onChanged={load} />)}</ul>
              )}
            </section>
            <Learned />
            {shared.length > 0 && (
              <section aria-labelledby="shared-title" className="mt-8">
                <h2 id="shared-title" className="text-[15px] font-semibold">Shared with you</h2>
                <ul className="divide-y divide-border">{shared.map((t) => <Row key={t.id} t={t} list={list} onChanged={load} />)}</ul>
              </section>
            )}
          </>
        )}
        <AnswerBank />
      </div>
    </div>
  );
}
