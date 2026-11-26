"use client";

import { ArrowUpRight, BookMarked, Lock, Pencil, Plus, Trash2 } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { MemoryPeopleSection } from "@/components/memory-people";
import { ShowAll } from "@/components/show-all";
import { refusal } from "@/components/templates";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { StoredAge } from "@/components/ui/stored-age";
import { useViewingAs } from "@/components/view-as";
import { recordName } from "@/components/work-log";
import { byDue, DoneButton, dueLine, todayET } from "@/components/your-commitments";
import { forgetMemory, getMemory, saveMemory } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { eastern, objectHref } from "@/lib/format";
import type { MemoryItem, MemoryItems, MemoryKind } from "@/lib/types";
import { useRead } from "@/lib/use-read";

const KINDS: { kind: MemoryKind; label: string; title: string; hint: string; example: string }[] = [
  {
    kind: "instruction",
    label: "Instructions",
    title: "Standing instructions",
    hint: "Rules for drafts and replies: sign-offs, greetings, who to copy, or senders Assist should skip.",
    example: "Sign off with Thanks, Ana. Always cc buying@petrasoap.com on supplier follow-ups.",
  },
  {
    kind: "preference",
    label: "Preferences",
    title: "Preferences",
    hint: "How you name customers, orders, or systems, and the wording you prefer.",
    example: "By \"the Kestrel order\" I mean the Kestrel Foods standing order.",
  },
  {
    kind: "commitment",
    label: "Commitments",
    title: "Commitments",
    hint: "Follow-ups you promised to do, kept until you mark them done.",
    example: "Follow up with Northwind on the label proof by Friday.",
  },
  {
    kind: "fact",
    label: "Facts",
    title: "Facts",
    hint: "Context about your work that the business systems do not record.",
    example: "Lot codes from the Mississauga line start with M.",
  },
];

const SOURCE_LABEL: Record<string, string> = {
  you: "Added by you",
  ask: "From a question in Ask",
  clarify: "From your clarification",
  assist: "From an Assist draft",
  autopilot: "From an approval or edit",
  feedback: "From your rating",
  action: "From an action you took",
  learn: "Learned from your edits",
  working: "From a conversation",
};

const FIELD =
  "w-full rounded-md border border-border-strong bg-bg px-3 py-2 text-[13px] leading-5 text-fg placeholder:text-faint focus:border-accent focus:outline-none";

function MemoryEditor({
  initial,
  onSaved,
  onCancel,
}: {
  initial?: MemoryItem;
  onSaved: () => void;
  onCancel?: () => void;
}) {
  const [kind, setKind] = useState<MemoryKind>((initial?.kind as MemoryKind) ?? "instruction");
  const [text, setText] = useState(initial?.text ?? "");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const selected = KINDS.find((k) => k.kind === kind) ?? KINDS[0];

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

  return (
    <div className="space-y-2.5" data-testid="assist-memory-form">
      <div className="flex flex-wrap items-center gap-1" role="radiogroup" aria-label="Memory type">
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            role="radio"
            aria-checked={kind === k.kind}
            onClick={() => setKind(k.kind)}
            className={cn(
              "h-7 rounded-md px-2.5 text-[12px] font-medium transition-colors cursor-pointer",
              kind === k.kind
                ? "bg-surface-2 text-fg ring-1 ring-border-strong"
                : "text-muted hover:bg-hover hover:text-fg",
            )}
          >
            {k.label}
          </button>
        ))}
      </div>
      <p className="text-[12px] text-muted">{selected.hint}</p>
      <textarea
        className={FIELD}
        rows={2}
        maxLength={1000}
        placeholder={selected.example}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "Enter" && void save()}
        data-testid="assist-memory-input"
      />
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={!text.trim() || busy}
          data-testid="assist-memory-save"
          className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-50 cursor-pointer"
        >
          {!initial && <Plus className="size-3.5" aria-hidden />}
          {initial ? "Save changes" : "Save to memory"}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="h-8 rounded-md px-2.5 text-[12.5px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
          >
            Cancel
          </button>
        )}
        {problem && (
          <p className="text-[12px] text-late" role="alert">
            {problem}
          </p>
        )}
      </div>
    </div>
  );
}

function MemoryRuleRow({ item, onChanged }: { item: MemoryItem; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const remove = async () => {
    if (removing) return;
    setRemoving(true);
    setProblem(null);
    try {
      await forgetMemory(item.id);
      onChanged();
    } catch (err) {
      setProblem(refusal(err));
      setRemoving(false);
    }
  };

  if (editing) {
    return (
      <li className="px-4 py-3.5">
        <MemoryEditor
          initial={item}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
          onCancel={() => setEditing(false)}
        />
      </li>
    );
  }

  return (
    <li className="flex items-start gap-3 px-4 py-3" data-testid="assist-memory-row">
      <div className="min-w-0 flex-1 space-y-1">
        <p className="whitespace-pre-wrap text-[13.5px] leading-5 text-fg">{item.text}</p>
        {(item.autopilot ?? []).map((rule) => (
          <p
            key={rule}
            className="inline-flex rounded-sm bg-accent-soft/75 px-1.5 py-0.5 text-[11.5px] font-medium text-accent"
            data-testid="assist-memory-autopilot"
          >
            {rule}
          </p>
        ))}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] leading-4 text-muted">
          {item.due && (
            <span className={item.due < todayET() ? "font-medium text-late" : "text-fg"}>
              {dueLine(item.due, todayET())}
            </span>
          )}
          <span>{SOURCE_LABEL[item.source] ?? "Remembered"}</span>
          <span aria-hidden>·</span>
          <span className="num text-faint">{eastern(item.at)}</span>
          <ShowAll items={(item.records ?? []).filter((id) => id.includes(":"))} first={4} noun="records">
            {(id) => (
              <Link href={objectHref(id.split(":", 1)[0], id)} className="text-fg/85 hover:underline">
                {recordName({ object_id: id, type: id.split(":", 1)[0] })}
              </Link>
            )}
          </ShowAll>
        </div>
        {problem && (
          <p className="text-[12px] text-late" role="alert">
            {problem}
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {item.kind === "commitment" && <DoneButton id={item.id} onDone={onChanged} />}
        <button
          type="button"
          onClick={() => setEditing(true)}
          data-testid="assist-memory-edit"
          className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
        >
          <Pencil className="size-3" aria-hidden />
          Edit
        </button>
        <button
          type="button"
          disabled={removing}
          onClick={() => void remove()}
          data-testid="assist-memory-remove"
          className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-late disabled:opacity-50 cursor-pointer"
        >
          <Trash2 className="size-3" aria-hidden />
          Remove
        </button>
      </div>
    </li>
  );
}

/**
 * Persistent Memory tab in /assist:
 * Clearly explains what Assist & Ask remember for the signed-in person and provides fast
 * add, filter, edit, and remove controls on clean Vercel neutral surfaces.
 */
export function AssistMemoryView() {
  const viewing = useViewingAs();
  const { data: list, error: problem, retry: load, at } = useRead<MemoryItems>("/api/memory", getMemory, !viewing);
  const [filter, setFilter] = useState<MemoryKind | "all">("all");

  const items = useMemo(() => list?.items ?? [], [list]);
  const counts = useMemo(() => {
    const map: Record<MemoryKind, number> = { instruction: 0, preference: 0, commitment: 0, fact: 0 };
    for (const item of items) {
      const k = item.kind as MemoryKind;
      if (k in map) map[k] += 1;
    }
    return map;
  }, [items]);

  const visibleKinds = filter === "all" ? KINDS : KINDS.filter((k) => k.kind === filter);

  return (
    <div className="mx-auto w-full max-w-[1040px] space-y-5 pb-10" data-testid="assist-memory-view">
      <section className="rounded-lg border border-border bg-surface p-4 shadow-[var(--shadow-card-val)]">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <div className="flex items-center gap-2">
              <BookMarked className="size-4 shrink-0 text-accent" aria-hidden />
              <h2 className="text-[15px] font-semibold tracking-[-0.015em] text-fg">
                Persistent memory
              </h2>
              <span className="inline-flex items-center gap-1 rounded-sm bg-surface-2 px-1.5 py-0.5 text-[11px] font-medium text-muted">
                <Lock className="size-3" aria-hidden />
                Private to you
              </span>
            </div>
            <p className="text-[13px] leading-5 text-muted">
              Your saved rules, preferences, commitments, and work facts. Assist follows these on every draft it prepares, and Ask applies them to your answers.
            </p>
          </div>
          <Link
            href="/memory"
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-border bg-bg px-2.5 text-[12px] font-medium text-fg transition-colors hover:bg-hover"
          >
            Full memory page
            <ArrowUpRight className="size-3.5 text-muted" aria-hidden />
          </Link>
        </div>
      </section>

      {viewing ? (
        <EmptyState
          title="Memory stays with its person."
          line="Viewing as someone shows none of what the Workbench remembers about them."
        />
      ) : (
        <>
          {problem && <LoadProblem message={plainError(problem)} onRetry={load} />}
          {!list && !problem && <Pending label="Reading your persistent memory" />}
          <StoredAge at={list ? at : null} />
          {list && !list.available && (
            <EmptyState
              title="Memory cannot be read right now."
              line="Your saved items are safe; try again in a few minutes."
            />
          )}

          {list?.available && (
            <>
              <section
                className="rounded-lg border border-border bg-surface p-4 shadow-[var(--shadow-card-val)]"
                aria-labelledby="assist-memory-add"
              >
                <h3 id="assist-memory-add" className="mb-2.5 text-[13.5px] font-semibold text-fg">
                  Add a rule, preference, or fact
                </h3>
                <MemoryEditor onSaved={load} />
              </section>

              <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Filter saved memory">
                <button
                  type="button"
                  aria-pressed={filter === "all"}
                  onClick={() => setFilter("all")}
                  className={cn(
                    "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] transition-colors cursor-pointer",
                    filter === "all"
                      ? "bg-surface-2 font-medium text-fg"
                      : "text-muted hover:bg-hover hover:text-fg",
                  )}
                >
                  All
                  <span className="num text-[11px] text-faint">{items.length}</span>
                </button>
                {KINDS.map((k) => (
                  <button
                    key={k.kind}
                    type="button"
                    aria-pressed={filter === k.kind}
                    onClick={() => setFilter(k.kind)}
                    className={cn(
                      "inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] transition-colors cursor-pointer",
                      filter === k.kind
                        ? "bg-surface-2 font-medium text-fg"
                        : "text-muted hover:bg-hover hover:text-fg",
                    )}
                  >
                    {k.label}
                    <span className="num text-[11px] text-faint">{counts[k.kind]}</span>
                  </button>
                ))}
              </div>

              {items.length === 0 ? (
                <div className="rounded-lg border border-border bg-surface p-8 text-center" data-testid="assist-memory-empty">
                  <BookMarked className="mx-auto mb-2 size-5 text-faint" aria-hidden />
                  <p className="text-[14px] font-medium text-fg">Nothing saved in your memory yet.</p>
                  <p className="mt-1 text-[13px] text-muted">
                    Add your first standing instruction above, or edit a draft in Ready for you so Assist can learn how you write.
                  </p>
                </div>
              ) : (
                <div className="space-y-4">
                  {visibleKinds.map(({ kind, title, hint }) => {
                    const kept = items.filter((i) => i.kind === kind);
                    const rows = kind === "commitment" ? byDue(kept) : kept;
                    if (rows.length === 0) {
                      if (filter === "all") return null;
                      return (
                        <div key={kind} className="rounded-lg border border-border bg-surface p-6 text-center text-[13px] text-muted">
                          No {title.toLowerCase()} saved yet.
                        </div>
                      );
                    }
                    return (
                      <section
                        key={kind}
                        className="overflow-hidden rounded-lg border border-border bg-surface shadow-[var(--shadow-card-val)]"
                        aria-labelledby={`assist-memory-group-${kind}`}
                      >
                        <div className="border-b border-border px-4 py-2.5">
                          <div className="flex items-baseline gap-2">
                            <h3 id={`assist-memory-group-${kind}`} className="text-[13px] font-semibold text-fg">
                              {title}
                            </h3>
                            <span className="num text-[11.5px] text-faint">{rows.length}</span>
                          </div>
                          <p className="mt-0.5 text-[12px] text-muted">{hint}</p>
                        </div>
                        <ul className="divide-y divide-border">
                          {rows.map((item) => (
                            <MemoryRuleRow key={item.id} item={item} onChanged={load} />
                          ))}
                        </ul>
                      </section>
                    );
                  })}
                </div>
              )}

              <div className="rounded-lg border border-border bg-surface p-4 shadow-[var(--shadow-card-val)]">
                <MemoryPeopleSection />
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
