"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { CornerDownLeft, MessageCircle, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { suggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useShortcut } from "@/lib/platform";
import { VERIFIED_EXAMPLES } from "@/lib/examples";
import { recordLabel, safeHref, typeLabel } from "@/lib/format";
import { stashPending } from "@/lib/pending";
import { arrange, type Arranged } from "./ask-box";
import { PlaceTile, SystemTile } from "./ui/glyphs";
import { Monogram } from "./ui/monogram";

/**
 * The command bar: one field that finds any record by number or name, goes anywhere in the
 * app and asks a question, all from the keyboard. ⌘K or Ctrl K opens it from anywhere, "/"
 * when no field has focus. ↑ ↓ move, ↵ opens, Esc closes.
 */

type Item =
  | { kind: "ask"; id: string; q: string }
  | { kind: "go"; id: string; label: string; href: string; hint: string }
  | { kind: "record"; id: string; s: Arranged }
  | { kind: "example"; id: string; q: string };

const PLACES: { label: string; href: string; hint: string; words: string }[] = [
  { label: "Today", href: "/", hint: "Late POs and what to chase", words: "today home front page briefing" },
  { label: "Late POs", href: "/late", hint: "Every PO past its needed-by date", words: "late orders purchase pos overdue" },
  { label: "Watching", href: "/watching", hint: "Every alert, rules and data health", words: "watching briefing alerts rules attention" },
  { label: "Ask", href: "/ask", hint: "New question", words: "ask question chat" },
  { label: "R&D projects", href: "/rd-projects", hint: "Sample and R&D projects, idle first", words: "rd r&d samples projects lab" },
  { label: "Activity", href: "/activity", hint: "What was sent, approved and logged", words: "activity actions log journal" },
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
  const { shortcut } = useShortcut();
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
      (p): Item => ({ kind: "go", id: `go-${p.href}`, label: p.label, href: p.href, hint: p.hint }),
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
  const typed = q.trim().length >= 2;
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(0_0_0/0.14)] animate-fade-in dark:bg-[rgb(0_0_0/0.45)]" />
        <Dialog.Content
          className="fixed top-[max(12px,12vh)] left-1/2 z-50 flex max-h-[min(540px,calc(100dvh-24px))] w-[min(660px,calc(100vw-24px))] -translate-x-1/2 flex-col overflow-hidden rounded-[14px] bg-raised shadow-[0_0_0_0.5px_rgb(0_0_0/0.14),0_24px_64px_-12px_rgb(0_0_0/0.3)] animate-fade-in focus:outline-none dark:shadow-[0_0_0_0.5px_rgb(255_255_255/0.12),0_24px_64px_-12px_rgb(0_0_0/0.7)]"
          aria-describedby={undefined}
          data-testid="command-palette"
        >
          <Dialog.Title className="sr-only">Find a record, go somewhere or ask</Dialog.Title>
          <div className="flex items-center gap-3 border-b border-[color-mix(in_srgb,var(--fg)_9%,transparent)] px-5">
            <Search className="size-[18px] shrink-0 text-muted" strokeWidth={2} aria-hidden />
            <input
              autoFocus
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(0);
              }}
              onKeyDown={onKeyDown}
              placeholder="Search records or ask a question"
              className="h-[58px] min-w-0 flex-1 bg-transparent text-[17px] tracking-[-0.01em] text-fg placeholder:text-faint focus:outline-none"
              role="combobox"
              aria-expanded
              aria-controls="palette-list"
              aria-activedescendant={flat[active] ? `palette-${flat[active].id}` : undefined}
              aria-label="Find a record, go somewhere or ask"
              data-testid="palette-input"
            />
            {searching && <span className="size-3.5 shrink-0 animate-spin rounded-full border-[1.5px] border-border-strong border-t-muted" aria-label="Searching" role="status" />}
            <kbd className="hidden h-5 items-center rounded-[5px] bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] px-1.5 font-sans text-[11px] text-muted sm:flex" aria-hidden>
              Esc
            </kbd>
          </div>
          <ul ref={list} id="palette-list" role="listbox" aria-label="Results" className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pt-1 pb-2">
            {items.map((g) => (
              <li key={g.group} role="presentation">
                <p className="px-3 pt-3 pb-1.5 text-[11px] leading-4 font-medium text-faint" role="presentation">
                  {g.group}
                </p>
                <ul role="presentation">
                  {g.items.map((it) => {
                    index += 1;
                    const i = index;
                    const on = i === active;
                    return (
                      <li
                        key={it.id}
                        id={`palette-${it.id}`}
                        role="option"
                        aria-selected={on}
                        data-index={i}
                        onMouseMove={() => !on && setActive(i)}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          choose(it);
                        }}
                        className={cn(
                          "flex h-10 cursor-pointer items-center gap-3 rounded-lg px-3 text-[13px]",
                          it.kind === "record" && "h-11",
                          on ? "bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] text-fg" : "text-fg/90",
                        )}
                        data-testid="palette-item"
                      >
                        <Row it={it} />
                        <span className={cn("ml-auto shrink-0 pl-2 text-[12px] text-muted", on ? "flex" : "hidden")} aria-hidden>
                          {it.kind === "ask" || it.kind === "example" ? "Ask" : "Open"}
                          <CornerDownLeft className="ml-1.5 size-3.5 self-center" strokeWidth={2} />
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </li>
            ))}
            {typed && !searching && records.length === 0 && (
              <li role="presentation" className="px-3 pt-3 text-[12px] leading-4 text-faint">
                No record by that name or number.
              </li>
            )}
          </ul>
          <div className="hidden h-10 shrink-0 items-center gap-4 border-t border-[color-mix(in_srgb,var(--fg)_9%,transparent)] bg-[color-mix(in_srgb,var(--fg)_2.5%,transparent)] px-4 text-[12px] text-muted sm:flex" aria-hidden>
            <span className="flex items-center gap-1.5">
              <Key>↑</Key>
              <Key>↓</Key>
              Move
            </span>
            <span className="flex items-center gap-1.5">
              <Key>↵</Key>
              Open
            </span>
            <span className="ml-auto flex items-center gap-1.5">
              <Key>{shortcut("K")}</Key>
              Anywhere
            </span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Key({ children }: { children: React.ReactNode }) {
  return <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded-[5px] bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] px-1 font-sans text-[11px] leading-none text-muted">{children}</kbd>;
}

const PARTY = /^(?:supplier|vendor|customer|contact|company)$/i;

function Row({ it }: { it: Item }) {
  if (it.kind === "ask" || it.kind === "example")
    return (
      <>
        {it.kind === "ask" ? (
          <PlaceTile href="/ask" />
        ) : (
          <span className="flex size-5 shrink-0 items-center justify-center rounded-[5.5px] bg-[color-mix(in_srgb,var(--fg)_7%,transparent)] text-muted" aria-hidden>
            <MessageCircle className="size-3" strokeWidth={2.2} />
          </span>
        )}
        <span className="min-w-0 truncate">
          {it.kind === "ask" && <span className="text-muted">Ask </span>}
          <span className={it.kind === "ask" ? "font-medium text-fg" : "text-fg"}>{it.q}</span>
        </span>
      </>
    );
  if (it.kind === "go")
    return (
      <>
        <PlaceTile href={it.href} />
        <span className="shrink-0 font-medium text-fg">{it.label}</span>
        <span className="hidden min-w-0 truncate text-[12px] text-muted sm:inline">{it.hint}</span>
      </>
    );
  const s = it.s;
  const party = PARTY.test(s.type);
  const sub = [typeLabel(s.type), s.facts ?? s.sublabel].filter(Boolean).join(" · ");
  return (
    <>
      {party ? <Monogram name={s.label} size={24} /> : <SystemTile system={s.systems[0] ?? "BC"} size={24} />}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] leading-[17px] font-medium text-fg">{recordLabel(s.type, s.label)}</span>
        <span className="block truncate text-[12px] leading-4 text-muted">{sub}</span>
      </span>
      {s.systems.length > 1 && (
        <span className="hidden shrink-0 items-center gap-0.5 sm:flex" aria-hidden>
          {s.systems.slice(1, 4).map((x) => (
            <SystemTile key={x} system={x} size={14} />
          ))}
        </span>
      )}
    </>
  );
}
