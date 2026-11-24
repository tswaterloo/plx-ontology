"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { refusal } from "@/components/templates";
import { useViewingAs } from "@/components/view-as";
import { byDue, DoneButton, dueLine, todayET } from "@/components/your-commitments";
import { MemoryPeopleSection } from "@/components/memory-people";
import { recordName } from "@/components/work-log";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { Toolbar } from "@/components/ui/toolbar";
import { forgetMemory, getMemory, saveMemory } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { eastern, objectHref } from "@/lib/format";
import type { MemoryItem, MemoryItems, MemoryKind } from "@/lib/types";
import { ShowAll } from "@/components/show-all";

const KINDS: { kind: MemoryKind; title: string; hint: string; example: string }[] = [
  { kind: "instruction", title: "Standing instructions", hint: "How every answer and draft of yours should go. A sign-off, a greeting or a sentence to leave out applies to your next drafts; who the Autopilot drafts nothing for, and who it copies, apply to its next drafts.", example: "Sign off with Thanks, Ana. Stop drafting replies to news@supplier.com. Always cc ana@company.com on supplier emails." },
  { kind: "preference", title: "Preferences", hint: "What you mean by a word, the systems and wording you prefer.", example: "By “the Kestrel order” I mean the Kestrel Foods standing order." },
  { kind: "commitment", title: "Commitments", hint: "What you said you would do, until it is done.", example: "Follow up with Northwind on the label proof by Friday." },
  { kind: "fact", title: "Facts", hint: "Things about your work the records don't say.", example: "Lot codes from the Mississauga line start with M." },
];

/** Where an item came from, in words. */
const SOURCE_LABEL: Record<string, string> = {
  you: "In your words",
  ask: "From a question of yours",
  clarify: "From your answer to a question back",
  assist: "From a draft you worked on",
  autopilot: "From your autopilot decisions",
  feedback: "From your rating of an answer",
  action: "From something you did",
  learn: "Learned from your edits and ratings",
  working: "From a conversation",
};

const field = "w-full rounded-md bg-bg px-2.5 py-1.5 text-[13px] leading-5 ring-1 ring-border-strong focus:outline-none focus:ring-accent";
const quiet = "h-7 rounded-md px-2 text-[12px] font-medium hover:bg-hover cursor-pointer disabled:cursor-default disabled:opacity-50";

/** The add/correct box: what to remember, as which kind. */
function MemoryForm({ initial, onSaved, onCancel }: { initial?: MemoryItem; onSaved: () => void; onCancel?: () => void }) {
  const [kind, setKind] = useState<MemoryKind>((initial?.kind as MemoryKind) ?? "instruction");
  const [text, setText] = useState(initial?.text ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await saveMemory({ kind, text: text.trim() }, initial?.id);
      if (!initial) setText("");
      onSaved();
    } catch (err) {
      setProblem(refusal(err));
    } finally {
      setBusy(false);
    }
  };
  const example = KINDS.find((k) => k.kind === kind)?.example;
  return (
    <div className="space-y-2" data-testid="memory-form">
      <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="Kind">
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            role="radio"
            aria-checked={kind === k.kind}
            className={`${quiet} ${kind === k.kind ? "bg-[color-mix(in_srgb,var(--fg)_8%,transparent)]" : "text-muted"}`}
            onClick={() => setKind(k.kind)}
          >
            {k.title.replace(/^Standing i/, "I")}
          </button>
        ))}
      </div>
      <textarea
        className={field}
        rows={2}
        maxLength={1000}
        placeholder={example}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "Enter" && save()}
        data-testid="memory-text"
      />
      <div className="flex items-center gap-2">
        <button type="button" className="h-8 rounded-lg bg-accent px-3 text-[13px] font-medium text-accent-fg cursor-pointer disabled:opacity-50" disabled={!text.trim() || busy} onClick={save} data-testid="memory-save">
          {initial ? "Save" : "Remember"}
        </button>
        {onCancel && (
          <button type="button" className={quiet} onClick={onCancel}>
            Cancel
          </button>
        )}
        {problem && <p className="text-[12px] text-muted" role="alert">{problem}</p>}
      </div>
    </div>
  );
}

/** One remembered item: its text, where it came from and when, the records it is about;
 * correct it or forget it. */
function Row({ item, onChanged }: { item: MemoryItem; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const forget = async () => {
    setProblem(null);
    try {
      await forgetMemory(item.id);
      onChanged();
    } catch (err) {
      setProblem(refusal(err));
    }
  };
  if (editing)
    return (
      <li className="py-3">
        <MemoryForm initial={item} onSaved={() => { setEditing(false); onChanged(); }} onCancel={() => setEditing(false)} />
      </li>
    );
  return (
    <li className="flex gap-3 py-3" data-testid="memory-row">
      <div className="min-w-0 flex-1">
        <p className="whitespace-pre-wrap text-[13px] leading-5">{item.text}</p>
        {(item.autopilot ?? []).map((rule) => (
          <p key={rule} className="mt-0.5 text-[12px] leading-4" data-testid="memory-autopilot">
            {rule}
          </p>
        ))}
        <p className="mt-0.5 flex flex-wrap gap-x-2 text-[12px] leading-4 text-muted">
          {item.due && <span className={item.due < todayET() ? "font-medium text-late" : "text-fg"}>{dueLine(item.due, todayET())}</span>}
          <span>{SOURCE_LABEL[item.source] ?? "Remembered"}</span>
          <span>{eastern(item.at)}</span>
          <ShowAll items={(item.records ?? []).filter((id) => id.includes(":"))} first={4} noun="records">
            {(id) => (
              <Link href={objectHref(id.split(":", 1)[0], id)} className="hover:underline">
                {recordName({ object_id: id, type: id.split(":", 1)[0] })}
              </Link>
            )}
          </ShowAll>
        </p>
        {problem && <p className="mt-1 text-[12px] text-muted" role="alert">{problem}</p>}
      </div>
      <div className="flex shrink-0 items-start gap-1">
        {item.kind === "commitment" && <DoneButton id={item.id} onDone={onChanged} />}
        <button type="button" className={quiet} onClick={() => setEditing(true)} data-testid="memory-edit">
          Correct
        </button>
        <button type="button" className={`${quiet} text-muted`} onClick={forget} data-testid="memory-forget">
          Forget
        </button>
      </div>
    </li>
  );
}

/** Memory: what the Workbench remembers about you and uses in every answer and draft of yours.
 * Only ever yours; correct anything, forget anything, or tell it something to keep. */
export default function MemoryPage() {
  return (
    <Suspense>
      <Memory />
    </Suspense>
  );
}

function Memory() {
  // ?record=<id> (from a record's Related panel): only what you keep about that record
  const record = useSearchParams()?.get("record") || null;
  const viewing = useViewingAs();
  const [list, setList] = useState<MemoryItems | null>(null);
  const [problem, setProblem] = useState<unknown>(null);
  const load = useCallback(() => {
    const c = new AbortController();
    setProblem(null);
    getMemory(c.signal)
      .then(setList)
      .catch((e) => !c.signal.aborted && setProblem(e));
    return () => c.abort();
  }, []);
  // viewing as someone reads nothing of theirs (the server refuses it too)
  useEffect(() => (viewing ? undefined : load()), [load, viewing]);
  const all = list?.items ?? [];
  const items = record ? all.filter((i) => (i.records ?? []).includes(record)) : all;
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-list">
      <Toolbar heading title="Memory" />
      <div className="w-full max-w-[60rem] overflow-y-auto px-6 py-5">
        <p className="mb-4 max-w-[70ch] text-[13px] leading-5 text-muted">
          What the Workbench remembers about how you work, so your answers and drafts fit you. Only you can see it. To add something, type &ldquo;from now on &hellip;&rdquo; or &ldquo;remind me to &hellip;&rdquo; in the Ask box.
        </p>
        {viewing ? (
          <EmptyState title="Memory stays with its person." line="Viewing as someone shows none of what the Workbench remembers about them." />
        ) : (
          <>
            {problem ? <LoadProblem message={plainError(problem)} onRetry={load} /> : null}
            {!list && !problem && <Pending label="Reading your memory" />}
            {list && !list.available && <EmptyState title="Memory can't be read right now." line="Nothing is lost. Try again in a few minutes." />}
            {list?.available && (
              <>
                <section className="mb-7 rounded-md bg-bg px-4 py-3.5" aria-labelledby="memory-new">
                  <h2 id="memory-new" className="mb-2 text-[15px] font-semibold">Tell it something to remember</h2>
                  <MemoryForm onSaved={load} />
                </section>
                {record && (
                  <p className="mb-5 text-[13px]" data-testid="memory-record-filter">
                    {items.length ? `What you keep about ${recordName({ object_id: record })}.` : `Nothing kept about ${recordName({ object_id: record })} yet.`}{" "}
                    <Link href="/memory" className="font-medium text-[color:var(--accent)] hover:underline">
                      Everything
                    </Link>
                  </p>
                )}
                {!record && items.length === 0 && <p className="text-[13px] text-muted">Nothing yet. It fills in as you ask, answer its questions, rate answers and edit drafts.</p>}
                {KINDS.map(({ kind, title, hint }) => {
                  const kept = items.filter((i) => i.kind === kind);
                  const rows = kind === "commitment" ? byDue(kept) : kept;
                  return rows.length ? (
                    <section key={kind} className="mb-6" aria-labelledby={`memory-${kind}`}>
                      <h2 id={`memory-${kind}`} className="text-[15px] font-semibold">
                        {title} <span className="font-normal text-muted">{rows.length}</span>
                      </h2>
                      <p className="text-[12px] text-muted">{hint}</p>
                      <ul className="divide-y divide-border">{rows.map((i) => <Row key={i.id} item={i} onChanged={load} />)}</ul>
                    </section>
                  ) : null;
                })}
                <MemoryPeopleSection />
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
