"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Bell, Home, ListChecks, MessageSquare, Search, Timer, type LucideIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { suggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { VERIFIED_EXAMPLES } from "@/lib/examples";
import { recordLabel, safeHref, typeLabel } from "@/lib/format";
import { stashPending } from "@/lib/pending";
import { arrange, type Arranged } from "./ask-box";

/**
 * The command bar: one field that finds any record by number or name, goes anywhere in the
 * app and asks a question, all from the keyboard. ⌘K or Ctrl K opens it from anywhere, "/"
 * when no field has focus. ↑ ↓ move, ↵ opens, Esc closes.
 */

type Item =
  | { kind: "ask"; id: string; q: string }
  | { kind: "go"; id: string; label: string; href: string; icon: LucideIcon; hint: string }
  | { kind: "record"; id: string; s: Arranged }
  | { kind: "example"; id: string; q: string };

const PLACES: { label: string; href: string; icon: LucideIcon; hint: string; words: string }[] = [
  { label: "Today", href: "/", icon: Home, hint: "Late POs and what to chase", words: "today home front page briefing" },
  { label: "Late POs", href: "/late", icon: Timer, hint: "Every PO past its needed-by date", words: "late orders purchase pos overdue" },
  { label: "Briefing", href: "/briefing", icon: Bell, hint: "Everything open, ranked", words: "briefing alerts attention" },
  { label: "Ask", href: "/ask", icon: MessageSquare, hint: "New question", words: "ask question chat" },
  { label: "Action log", href: "/actions", icon: ListChecks, hint: "Emails sent and tasks logged", words: "actions log journal" },
];

const PaletteCtx = createContext<{ open: () => void }>({ open: () => undefined });

/** Opens the command bar from anywhere under the provider (the top bar's search, a hint). */
export function useCommandPalette() {
  return useContext(PaletteCtx);
}

export function CommandPaletteProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const typing = e.target instanceof HTMLElement && (e.target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName));
      if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey)) {
        e.preventDefault();
        setOpen((v) => !v || e.key === "/");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const value = useMemo(() => ({ open: () => setOpen(true) }), []);
  return (
    <PaletteCtx.Provider value={value}>
      {children}
      <CommandPalette open={open} onOpenChange={setOpen} />
    </PaletteCtx.Provider>
  );
}

function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [records, setRecords] = useState<Arranged[]>([]);
  const [searching, setSearching] = useState(false);
  const [active, setActive] = useState(0);
  const list = useRef<HTMLUListElement>(null);

  // reset on open; records as you type (debounced, the stale request cancelled)
  useEffect(() => {
    if (open) {
      setQ("");
      setRecords([]);
      setActive(0);
    }
  }, [open]);
  useEffect(() => {
    const text = q.trim();
    if (!open || text.length < 2) {
      setRecords([]);
      setSearching(false);
      return;
    }
    const c = new AbortController();
    setSearching(true);
    const t = setTimeout(() => {
      suggest(text, c.signal)
        .then((r) => setRecords(Array.isArray(r.suggestions) ? arrange(r.suggestions, text).slice(0, 7) : []))
        .catch(() => undefined)
        .finally(() => !c.signal.aborted && setSearching(false));
    }, 120);
    return () => {
      clearTimeout(t);
      c.abort();
    };
  }, [q, open]);

  const items: { group: string; items: Item[] }[] = useMemo(() => {
    const text = q.trim();
    const low = text.toLowerCase();
    const places = PLACES.filter((p) => !low || p.words.includes(low) || p.label.toLowerCase().includes(low)).map(
      (p): Item => ({ kind: "go", id: `go-${p.href}`, label: p.label, href: p.href, icon: p.icon, hint: p.hint }),
    );
    if (!text)
      return [
        { group: "Go to", items: places },
        { group: "Ask", items: VERIFIED_EXAMPLES.slice(0, 4).map((x, i): Item => ({ kind: "example", id: `ex-${i}`, q: x })) },
      ];
    return [
      { group: "Ask", items: [{ kind: "ask", id: "ask", q: text } as Item] },
      { group: "Records", items: records.map((s, i): Item => ({ kind: "record", id: `rec-${s.object_id}-${i}`, s })) },
      { group: "Go to", items: places },
    ].filter((g) => g.items.length > 0);
  }, [q, records]);
  const flat = useMemo(() => items.flatMap((g) => g.items), [items]);
  useEffect(() => setActive((a) => Math.min(a, Math.max(0, flat.length - 1))), [flat.length]);
  useEffect(() => {
    list.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const choose = useCallback(
    (it: Item | undefined) => {
      if (!it) return;
      onOpenChange(false);
      if (it.kind === "go") router.push(it.href);
      else if (it.kind === "record") router.push(safeHref(it.s.open_url) ?? "/");
      else {
        stashPending(it.q, []);
        router.push(`/ask?q=${encodeURIComponent(it.q)}`);
      }
    },
    [onOpenChange, router],
  );

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) {
      e.preventDefault();
      setActive((a) => (flat.length ? (a + 1) % flat.length : 0));
    } else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) {
      e.preventDefault();
      setActive((a) => (flat.length ? (a - 1 + flat.length) % flat.length : 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(flat[active]);
    }
  }

  let index = -1;
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(0_0_0/0.25)]" />
        <Dialog.Content
          className="fixed top-[12vh] left-1/2 z-50 flex max-h-[min(560px,76vh)] w-[min(640px,calc(100vw-24px))] -translate-x-1/2 flex-col overflow-hidden rounded border border-border-strong bg-raised shadow-pop focus:outline-none"
          aria-describedby={undefined}
          data-testid="command-palette"
        >
          <Dialog.Title className="sr-only">Find a record, go somewhere or ask</Dialog.Title>
          <div className="flex items-center gap-3 border-b border-border px-4">
            <Search className="size-4 shrink-0 text-faint" strokeWidth={1.75} aria-hidden />
            <input
              autoFocus
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(0);
              }}
              onKeyDown={onKeyDown}
              placeholder="PO, SO, supplier, customer or lot number, or a question"
              className="h-13 min-w-0 flex-1 bg-transparent text-[16px] text-fg placeholder:text-faint focus:outline-none"
              role="combobox"
              aria-expanded
              aria-controls="palette-list"
              aria-activedescendant={flat[active] ? `palette-${flat[active].id}` : undefined}
              aria-label="Find a record, go somewhere or ask"
              data-testid="palette-input"
            />
            {searching && <span className="text-meta text-faint">Searching…</span>}
            <kbd className="hidden rounded-sm border border-border px-1 font-mono text-[11px] leading-4 text-faint sm:block">esc</kbd>
          </div>
          <ul ref={list} id="palette-list" role="listbox" aria-label="Results" className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1.5">
            {items.map((g) => (
              <li key={g.group} role="presentation">
                <p className="px-2.5 pt-2.5 pb-1 text-label text-faint" role="presentation">
                  {g.group}
                </p>
                <ul role="presentation">
                  {g.items.map((it) => {
                    index += 1;
                    const i = index;
                    return (
                      <li
                        key={it.id}
                        id={`palette-${it.id}`}
                        role="option"
                        aria-selected={i === active}
                        data-index={i}
                        onMouseMove={() => i !== active && setActive(i)}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          choose(it);
                        }}
                        className={cn(
                          "relative flex min-h-10 cursor-pointer items-center gap-3 rounded px-2.5 py-1.5 text-row",
                          i === active ? "bg-surface-2 text-fg" : "text-muted",
                        )}
                        data-testid="palette-item"
                      >
                        {i === active && <span className="absolute inset-y-1.5 left-0 w-0.5 bg-accent" aria-hidden />}
                        <Row it={it} />
                        <span className={cn("ml-auto shrink-0 text-faint", i === active ? "opacity-100" : "opacity-0")} aria-hidden>
                          <span className="text-[11px]">Enter</span>
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
            {q.trim().length >= 2 && !searching && records.length === 0 && (
              <li role="presentation" className="px-2.5 py-2 text-meta text-faint">
                No record by that name or number; ask it as a question.
              </li>
            )}
          </ul>
          <div className="hidden items-center gap-4 border-t border-border px-4 py-2 text-[11px] text-faint sm:flex" aria-hidden>
            <span>
              <kbd className="font-mono">↑↓</kbd> move
            </span>
            <span>
              <kbd className="font-mono">↵</kbd> open
            </span>
            <span className="ml-auto">
              <kbd className="font-mono">⌘K</kbd> anywhere
            </span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Row({ it }: { it: Item }) {
  if (it.kind === "ask" || it.kind === "example")
    return (
      <>
        <span className="min-w-0 truncate">
          {it.kind === "ask" && <span className="text-faint">Ask </span>}
          <span className="text-fg">{it.q}</span>
        </span>
      </>
    );
  if (it.kind === "go")
    return (
      <>
        <span className="text-fg">{it.label}</span>
        <span className="hidden truncate text-meta text-faint sm:inline">{it.hint}</span>
      </>
    );
  const s = it.s;
  const sub = [typeLabel(s.type), s.facts ?? s.sublabel].filter(Boolean).join(" · ");
  return (
    <>
      <span className="min-w-0 flex-1 truncate">
        <span className="font-medium text-fg">{recordLabel(s.type, s.label)}</span>
        <span className="ml-2 text-meta text-faint">{sub}</span>
      </span>
    </>
  );
}
