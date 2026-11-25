"use client";

import { ArrowUp, Box, Brain, Building2, ExternalLink, Factory, FileBox, FlaskConical, Hash, MapPin, Package, Paperclip, Square, Truck, User, X, type LucideIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { suggest as fetchSuggest, warmAsk } from "@/lib/api";
import { ACCEPT, addFiles, dropFile, sizeWords, type Attachment } from "@/lib/attachments";
import { cn } from "@/lib/cn";
import { forget, recall, remember } from "@/lib/tab-memory";
import { plural, recordLabel, safeHref, typeLabel } from "@/lib/format";
import type { Suggestion } from "@/lib/types";
import { SystemCode } from "./ui/glyphs";

export interface Pin {
  object_id: string;
  type: string;
  label: string;
}

export const SUGGEST_DEBOUNCE_MS = 150;
const MIN_CHARS = 2;
/** Room the list needs below the box; with less, it opens upward. */
const DROP_ROOM = 360;

const TYPE_ICON: Record<string, LucideIcon> = {
  purchaseorder: FileBox,
  salesorder: Package,
  customer: Building2,
  customerlocation: MapPin,
  supplier: Truck,
  vendor: Truck,
  item: Box,
  product: Box,
  lot: Hash,
  batch: FlaskConical,
  assemblyorder: Factory,
  manufacturingorder: Factory,
  person: User,
  employee: User,
};

/** The icon of a record type, whichever spelling the API sends ("purchase_order", "PurchaseOrder"). */
export function typeIcon(type: string): LucideIcon {
  return TYPE_ICON[type.replace(/[_\s-]/g, "").toLowerCase()] ?? Hash;
}

const SYSTEMS = ["BC", "Portal", "Sage", "FileMaker"];

/** "Purchase order · V06110 · BC" -> facts ("V06110") and systems (["BC"]); the type word goes. */
export function splitSublabel(sub: string | null | undefined, type: string): { facts: string; systems: string[] } {
  const parts = (sub ?? "").split(" · ").map((p) => p.trim()).filter(Boolean);
  const kind = typeLabel(type).toLowerCase();
  const systems: string[] = [];
  const facts: string[] = [];
  for (const p of parts) {
    const sys = p.split(/\s+and\s+/).filter((x) => SYSTEMS.includes(x));
    if (sys.length && sys.length === p.split(/\s+and\s+/).length) systems.push(...sys);
    else if (p.toLowerCase() !== kind && p.toLowerCase() !== type.toLowerCase()) facts.push(p);
  }
  return { facts: facts.join(" · "), systems };
}

export type Arranged = Suggestion & { systems: string[]; facts: string; shared?: number };

/**
 * The list as a reader wants it: one row per entity (same name and type family; the BC
 * record is the target, the other systems become dots; rows merge only on the server's `same_as`), and, when the typed words name a
 * kind ("po 1060"), records of that kind first. Order is otherwise the server's. Records
 * of one name the server kept apart (each with its own code) stay apart, side by side,
 * the first of them carrying how many share the name (`shared`).
 */
export function arrange(list: Suggestion[], query: string): Arranged[] {
  const family = (t: string) => t.replace(/[_\s-]/g, "").toLowerCase().replace(/^vendor$/, "supplier").replace(/^product$/, "item");
  const name = (s: Suggestion) => `${family(s.type)}|${s.label.trim().toLowerCase()}`;
  const rowsOf = new Map<string, Arranged[]>(); // by name: each record of that name
  for (const s of list) {
    const { facts, systems } = splitSublabel(s.sublabel, s.type);
    const same = rowsOf.get(name(s)) ?? [];
    rowsOf.set(name(s), same);
    // two rows are one record only when the server says so (the same id, or one lists the
    // other in same_as): a shared name, or a missing code, never is
    const i = same.findIndex((r) => r.object_id === s.object_id || (r.same_as ?? []).includes(s.object_id) || (s.same_as ?? []).includes(r.object_id));
    if (i < 0) {
      same.push({ ...s, systems, facts });
      continue;
    }
    const have = same[i];
    const merged = [...new Set([...have.systems, ...systems])];
    // the BC record is the one opened; its facts lead
    if (systems.includes("BC") && !have.systems.includes("BC")) same[i] = { ...s, systems: merged, facts: facts || have.facts };
    else have.systems = merged;
  }
  // records of one name side by side, at the first one's place
  const rows = [...rowsOf.values()].flatMap((same) => (same.length > 1 ? [{ ...same[0], shared: same.length }, ...same.slice(1)] : same));
  const q = query.toLowerCase();
  const wants = /\bpo\b|purchase/.test(q) ? "purchaseorder" : /\bso\b|sales order/.test(q) ? "salesorder" : /\bao\b|assembly/.test(q) ? "assemblyorder" : null;
  if (!wants) return rows;
  return [...rows.filter((r) => family(r.type) === wants), ...rows.filter((r) => family(r.type) !== wants)];
}

/** "2 customers share this name" (the type's plural, lower case). */
export function sharedName(n: number, type: string): string {
  // orders are known by their number, everything else by its name
  const by = /order$/i.test(typeLabel(type)) ? "number" : "name";
  return `${n} ${plural(n, typeLabel(type).toLowerCase()).replace(/^\d+\s+/, "")} share this ${by}`;
}

/** The record being named: text after an "@" (at the start or after a space) up to the caret,
 * and the span it takes (the "@" included, and the rest of the word the caret sits in).
 * With no "@" before the caret there is no mention, so nothing is looked up. */
export function mentionAt(text: string, caret: number): { start: number; end: number; text: string } | null {
  const before = text.slice(0, caret);
  const m = before.match(/(?:^|\s)@([^@\n?!;]*)$/u);
  if (!m) return null;
  const start = caret - m[1].length - 1;
  const tail = text.slice(caret).match(/^[\p{L}\p{N}\-/#.&']*/u);
  return { start, end: caret + (tail ? tail[0].length : 0), text: m[1] };
}

export function AskBox({
  onSubmit,
  size = "lg",
  placeholder = "Ask a question, or ask for a chart, document or quote",
  autoFocus,
  disabled,
  busy,
  onStop,
  initial = "",
  dropUp = false,
  draftKey,
  inputId,
  label = "Ask a question",
  attachable = false,
  deepable = false,
}: {
  onSubmit: (question: string, pins: Pin[], files: Attachment[], opts?: { deep?: boolean }) => void;
  /** Shows the "Think it through" toggle: the question is then asked with the deeper read. */
  deepable?: boolean;
  /** Files and images can be attached (a button, or pasted into the box). */
  attachable?: boolean;
  size?: "lg" | "md" | "sm";
  /** An id for the text field (the top bar's search is focused by ⌘K / "/"). */
  inputId?: string;
  /** The field's accessible name. */
  label?: string;
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  busy?: boolean;
  onStop?: () => void;
  initial?: string;
  /** Open the suggestions above the box (a box at the bottom of the screen). */
  dropUp?: boolean;
  /** Keeps the unsent text in this tab (sessionStorage) under this key, so it is there when the person comes back. */
  draftKey?: string;
}) {
  const router = useRouter();
  const [queued, setQueued] = useState<{ q: string; pins: Pin[]; files: Attachment[]; deep: boolean } | null>(null);
  const [deep, setDeep] = useState(false);
  const [value, setValue] = useState(initial);
  const [pins, setPins] = useState<Pin[]>([]);
  const [items, setItems] = useState<(Suggestion & { systems?: string[]; facts?: string; shared?: number })[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [frag, setFrag] = useState({ start: 0, end: 0, text: "" });
  const [up, setUp] = useState(dropUp);
  const [, setSearching] = useState(false);
  const [files, setFiles] = useState<Attachment[]>([]);
  const [fileNote, setFileNote] = useState("");
  // reads in flight (a slow file): a question waits for them, and a read that finishes after
  // the box was sent or cleared is dropped, never attached to the next question
  const [reading, setReading] = useState(0);
  const filesRef = useRef<Attachment[]>([]);
  const generation = useRef(0);
  const readChain = useRef<Promise<void>>(Promise.resolve());
  filesRef.current = files;
  const picker = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  const cache = useRef(new Map<string, Suggestion[]>());
  const listId = useId();

  // an unsent question is kept for the tab and comes back with it
  useEffect(() => {
    if (draftKey && !initial) {
      const d = recall(draftKey, (v): v is string => typeof v === "string");
      if (d) setValue((cur) => cur || d);
    }
  }, [draftKey, initial]);
  // written as it is typed; cleared where the text is emptied (an edit to nothing, a send)
  useEffect(() => {
    if (draftKey && value) remember(draftKey, value);
  }, [draftKey, value]);

  // Auto-grow the textarea up to a cap.
  useLayoutEffect(() => {
    const fit = () => {
      const el = ref.current;
      if (!el) return;
      el.style.height = "auto";
      // empty: tall enough for the placeholder, which wraps on a phone (it never counts in scrollHeight)
      let h = el.scrollHeight;
      if (!el.value && el.placeholder) {
        el.value = el.placeholder;
        h = el.scrollHeight;
        el.value = "";
      }
      el.style.height = `${Math.min(h, 220)}px`;
    };
    fit();
    window.addEventListener("resize", fit); // a rotated phone wraps the text anew
    return () => window.removeEventListener("resize", fit);
  }, [value]);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
      ctrl.current?.abort();
    },
    [],
  );

  const close = useCallback(() => {
    setOpen(false);
    setActive(-1);
  }, []);

  // Open upward when the box sits near the bottom of the screen (a docked composer).
  const place = useCallback(() => {
    const r = boxRef.current?.getBoundingClientRect();
    if (!r || typeof window === "undefined") return;
    setUp(dropUp || window.innerHeight - r.bottom < DROP_ROOM);
  }, [dropUp]);

  const lookup = useCallback(
    (text: string, caret: number) => {
      const f = mentionAt(text, caret);
      if (timer.current) clearTimeout(timer.current);
      ctrl.current?.abort();
      // suggestions open only after an "@"; with none, there is no popup at all
      if (!f) {
        close();
        setItems([]);
        return;
      }
      setFrag(f);
      const q = f.text.trim();
      // numbers from the first digit ("1" of "po 1"), words from two letters
      const min = /\d$/.test(q) ? 1 : MIN_CHARS;
      if (q.length < min) {
        close();
        setItems([]);
        return;
      }
      place();
      const hit = cache.current.get(q.toLowerCase());
      if (hit) {
        setItems(hit);
        setOpen(hit.length > 0);
        setActive(-1);
        return;
      }
      timer.current = setTimeout(async () => {
        const c = new AbortController();
        ctrl.current = c;
        const slow = setTimeout(() => !c.signal.aborted && setSearching(true), 300);
        try {
          const res = await fetchSuggest(q, c.signal);
          if (c.signal.aborted) return;
          const list = Array.isArray(res.suggestions) ? arrange(res.suggestions, text).slice(0, 8) : [];
          cache.current.set(q.toLowerCase(), list);
          setItems(list);
          setOpen(list.length > 0);
          setActive(-1);
        } catch {
          // Suggestions are a convenience: a failed lookup just shows none.
          if (!c.signal.aborted) close();
        } finally {
          clearTimeout(slow);
          setSearching(false);
        }
      }, SUGGEST_DEBOUNCE_MS);
    },
    [close],
  );

  /** Open the record (the suggestion's own page). */
  function openRecord(s: Suggestion) {
    const href = safeHref(s.open_url);
    close();
    if (href) router.push(href);
  }

  /** Ask about it: its name replaces what was typed for it, and it is pinned. */
  function pick(s: Suggestion) {
    const label = recordLabel(s.type, s.label); // "PO PO100040" is said "PO 100040"
    const span = frag; // "@" and what was typed after it: the name replaces them
    const next = value.slice(0, span.start) + label + " " + value.slice(span.end).replace(/^\s+/, "");
    setValue(next);
    setPins((p) => (p.some((x) => x.object_id === s.object_id) ? p : [...p, { object_id: s.object_id, type: s.type, label }]));
    close();
    setItems([]);
    const caret = span.start + label.length + 1;
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(caret, caret);
    });
  }

  function attach(list: Iterable<File>) {
    const mine = generation.current;
    const batch = [...list];
    setReading((n) => n + 1);
    // one batch at a time, each checked against the files as they are when it starts
    readChain.current = readChain.current.then(async () => {
      try {
        if (mine !== generation.current) return;
        const have = filesRef.current;
        const { next, problems } = await addFiles(batch, have);
        if (mine !== generation.current) return;
        const added = next.slice(have.length);
        // merged into what is there now, so a file removed meanwhile stays removed
        setFiles((cur) => [...cur, ...added]);
        filesRef.current = [...filesRef.current, ...added];
        setFileNote(problems.join(" "));
      } finally {
        setReading((n) => n - 1);
      }
    });
    return readChain.current;
  }

  function submit() {
    // a question with only files asks what they show
    const q = value.trim() || (files.length ? "What does this show?" : "");
    if (!q || disabled || reading > 0) return;
    const live = pins.filter((p) => q.includes(p.label));
    // while an answer is being written the question waits and is asked the moment it ends
    if (busy) setQueued({ q, pins: live, files, deep });
    else if (deepable && deep) onSubmit(q, live, files, { deep: true });
    else onSubmit(q, live, files);
    setDeep(false);
    generation.current += 1;
    filesRef.current = [];
    setValue("");
    if (draftKey) forget(draftKey);
    setPins([]);
    setFiles([]);
    setFileNote("");
    close();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (open && items.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => (i + 1) % items.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => (i <= 0 ? items.length - 1 : i - 1));
        return;
      }
      if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        pick(items[active]);
        return;
      }
      if (e.key === "Tab" && active >= 0) {
        e.preventDefault();
        pick(items[active]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
    }
    if (e.key === "Escape" && busy && onStop) {
      e.preventDefault();
      onStop();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  useEffect(() => {
    if (busy || disabled || !queued) return;
    const next = queued;
    setQueued(null);
    if (next.deep) onSubmit(next.q, next.pins, next.files, { deep: true });
    else onSubmit(next.q, next.pins, next.files);
  }, [busy, disabled, queued, onSubmit]);

  const big = size === "lg";
  const sm = size === "sm";
  const pop = "absolute inset-x-0 z-40 rounded-lg border border-border-strong bg-raised shadow-pop";
  return (
    <div className="relative w-full" ref={boxRef}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className={cn(
          "group relative flex flex-col bg-bg transition-colors dark:bg-surface",
          sm
            ? "rounded-lg border border-border-strong focus-within:border-accent/65"
            : "rounded-xl border border-border-strong focus-within:border-accent/65",
          big ? "px-3.5 pt-2.5 pb-2.5" : sm ? "px-2 py-1" : "px-3 pt-2 pb-2",
        )}
      >
        {attachable && files.length > 0 && (
          <ul className="flex flex-wrap gap-1 px-1.5 pt-0.5 pb-1" aria-label="Attachments" data-testid="attachments">
            {files.map((f) => (
              <li key={f.id} className="inline-flex h-6 items-center gap-1.5 rounded-md bg-surface-2 pl-2 text-[12px] text-fg">
                <span className="max-w-48 truncate">{f.name}</span>
                <span className="text-muted">{sizeWords(f.size)}</span>
                <button
                  type="button"
                  aria-label={`Remove ${f.name}`}
                  onClick={() => {
                    dropFile(f);
                    filesRef.current = filesRef.current.filter((x) => x.id !== f.id);
                    setFiles((cur) => cur.filter((x) => x.id !== f.id));
                  }}
                  className="flex size-6 items-center justify-center rounded-md text-muted hover:text-fg cursor-pointer"
                >
                  <X className="size-3" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
        {pins.length > 0 && (
          <div className="flex flex-wrap gap-1 px-1.5 pt-0.5 pb-1" aria-label="Picked items">
            {pins.map((p) => {
              const Icon = typeIcon(p.type);
              return (
                <span key={p.object_id} className="inline-flex h-6 items-center gap-1.5 rounded-md bg-surface-2 pl-2 text-[12px] text-fg">
                  <Icon className="size-3 text-muted" strokeWidth={2.2} aria-hidden />
                  {p.label}
                  <button
                    type="button"
                    aria-label={`Remove ${p.label}`}
                    onClick={() => setPins(pins.filter((x) => x.object_id !== p.object_id))}
                    className="flex size-6 items-center justify-center rounded-md text-muted hover:text-fg cursor-pointer"
                  >
                    <X className="size-3" aria-hidden />
                  </button>
                </span>
              );
            })}
          </div>
        )}
        <div className={cn("flex gap-2", sm ? "items-end" : "flex-col")}>
          <textarea
            ref={ref}
            id={inputId}
            value={value}
            rows={1}
            autoFocus={autoFocus}
            disabled={disabled}
            placeholder={placeholder}
            aria-label={label}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={open}
            aria-controls={listId}
            aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
            onChange={(e) => {
              setValue(e.target.value);
              if (draftKey && !e.target.value) forget(draftKey);
              lookup(e.target.value, e.target.selectionStart ?? e.target.value.length);
            }}
            onKeyDown={onKeyDown}
            onPaste={(e) => {
              if (!attachable) return;
              const pasted = Array.from(e.clipboardData?.files ?? []);
              if (pasted.length === 0) return;
              e.preventDefault();
              void attach(pasted);
            }}
            onFocus={() => warmAsk()}
            onBlur={() => setTimeout(close, 120)}
            className={cn(
              "max-h-[220px] flex-1 resize-none bg-transparent px-1.5 text-fg placeholder:text-faint outline-none focus:outline-none focus-visible:outline-none",
              sm ? "min-h-[36px] py-2 text-[14px] leading-5" : big ? "min-h-[72px] py-2.5 text-[15px] leading-6" : "min-h-[38px] py-1.5 text-[14px] leading-5",
            )}
          />
          {sm ? (
            <>
          {attachable && (
            <>
              <input
                ref={picker}
                type="file"
                multiple
                accept={ACCEPT}
                hidden
                data-testid="attach-input"
                onChange={(e) => {
                  void attach(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => picker.current?.click()}
                disabled={disabled || busy}
                aria-label="Attach files or images"
                title="Attach a PDF, spreadsheet or image (or paste one)"
                className={cn("flex shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer disabled:opacity-50", sm ? "m-0.5 size-[30px]" : "size-9")}
              >
                <Paperclip className="size-4" strokeWidth={2} aria-hidden />
              </button>
            </>
          )}
          {busy && onStop && !value.trim() && files.length === 0 ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop"
              title="Stop"
              className={cn("flex shrink-0 items-center justify-center rounded-lg bg-fg text-bg hover:opacity-85 cursor-pointer", sm ? "m-0.5 size-[30px]" : "size-9")}
            >
              <Square className="size-3 fill-current" aria-hidden />
            </button>
          ) : (
            <button
              type="submit"
              aria-label="Ask"
              title="Ask (Enter)"
              disabled={(!value.trim() && files.length === 0) || disabled || reading > 0}
              aria-disabled={(!value.trim() && files.length === 0) || disabled || reading > 0}
              className={cn(
                "flex shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg shadow-sm hover:brightness-110 disabled:bg-surface-2 disabled:text-faint disabled:shadow-none cursor-pointer disabled:cursor-default",
                sm ? "m-0.5 size-[30px]" : "size-9",
              )}
            >
              <ArrowUp className="size-4" strokeWidth={2.4} aria-hidden />
            </button>
          )}
            </>
          ) : (
            <div className="flex items-center gap-1 px-0.5 pb-0.5">
          {attachable && (
            <>
              <input
                ref={picker}
                type="file"
                multiple
                accept={ACCEPT}
                hidden
                data-testid="attach-input"
                onChange={(e) => {
                  void attach(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => picker.current?.click()}
                disabled={disabled || busy}
                aria-label="Attach files or images"
                title="Attach a PDF, spreadsheet or image (or paste one)"
                className={cn("flex shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer disabled:opacity-50", sm ? "m-0.5 size-[30px]" : "size-9")}
              >
                <Paperclip className="size-4" strokeWidth={2} aria-hidden />
              </button>
            </>
          )}
          {deepable && (
            <button
              type="button"
              role="switch"
              aria-checked={deep}
              onClick={() => setDeep((d) => !d)}
              title="Takes longer and uses deeper analysis"
              data-testid="deep-toggle"
              className={cn("flex h-9 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-[13px] cursor-pointer", deep ? "bg-accent/15 text-accent font-medium" : "text-muted hover:bg-hover hover:text-fg")}
            >
              <Brain className="size-4" strokeWidth={2} aria-hidden />
              Deep research
            </button>
          )}
              <span className="flex-1" />
          {busy && onStop && !value.trim() && files.length === 0 ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop"
              title="Stop"
              className={cn("flex shrink-0 items-center justify-center rounded-lg bg-fg text-bg hover:opacity-85 cursor-pointer", sm ? "m-0.5 size-[30px]" : "size-9")}
            >
              <Square className="size-3 fill-current" aria-hidden />
            </button>
          ) : (
            <button
              type="submit"
              aria-label="Ask"
              title="Ask (Enter)"
              disabled={(!value.trim() && files.length === 0) || disabled || reading > 0}
              aria-disabled={(!value.trim() && files.length === 0) || disabled || reading > 0}
              className={cn(
                "flex shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg shadow-sm hover:brightness-110 disabled:bg-surface-2 disabled:text-faint disabled:shadow-none cursor-pointer disabled:cursor-default",
                sm ? "m-0.5 size-[30px]" : "size-9",
              )}
            >
              <ArrowUp className="size-4" strokeWidth={2.4} aria-hidden />
            </button>
          )}
            </div>
          )}
        </div>
      </form>
      <ul
        id={listId}
        role="listbox"
        aria-label="Suggestions"
        hidden={!open || items.length === 0}
        className={cn(pop, "max-h-80 overflow-auto p-1.5", up ? "bottom-full mb-2" : "top-full mt-2")}
        data-testid="suggestions"
        data-placement={up ? "up" : "down"}
      >
        <li role="presentation" className="px-2 pt-1 pb-1.5 text-[12px] font-medium text-muted">
          Records
        </li>
        {items.map((s, i) => {
          const sub = [typeLabel(s.type), s.facts ?? s.sublabel].filter(Boolean).join(" · ");
          const Icon = typeIcon(s.type);
          return (
            <Fragment key={`${s.object_id}-${i}`}>
            {s.shared && s.shared > 1 ? (
              <li role="presentation" className="px-2 pt-2 pb-1 text-[12px] text-muted" data-testid="shared-name">
                {sharedName(s.shared, s.type)}
              </li>
            ) : null}
            <li
              key={`${s.object_id}-${i}`}
              id={`${listId}-${i}`}
              role="option"
              aria-selected={i === active}
              onMouseDown={(e) => {
                e.preventDefault();
                pick(s);
              }}
              onMouseEnter={() => setActive(i)}
              className={cn("flex min-h-11 cursor-pointer items-center gap-3 rounded-lg px-2 py-1.5 text-[13px]", i === active ? "bg-hover" : "")}
              data-type={s.type}
            >
              <span className="flex size-5 shrink-0 items-center justify-center text-muted" aria-hidden>
                <Icon className="size-3.5" strokeWidth={2} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-fg">{recordLabel(s.type, s.label)}</span>
                <span className="block truncate text-[12px] leading-4 text-muted">{sub}</span>
              </span>
              {(s.systems ?? []).length > 0 && (
                <span className="hidden shrink-0 items-center gap-1 sm:flex" aria-label={`Found in ${s.systems!.join(" and ")}`}>
                  {s.systems!.map((x) => (
                    <SystemCode key={x} system={x} size={16} />
                  ))}
                </span>
              )}
              <button
                type="button"
                tabIndex={-1}
                aria-label={`Open ${recordLabel(s.type, s.label)}`}
                title="Open this record"
                onMouseDown={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  openRecord(s);
                }}
                className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
                data-testid="open-record"
              >
                <ExternalLink className="size-3.5" strokeWidth={2} aria-hidden />
              </button>
            </li>
            </Fragment>
          );
        })}
      </ul>
      {reading > 0 && (
        <p className="px-2 pt-1 text-[12px] text-muted" role="status" data-testid="attach-reading">
          Reading {reading === 1 ? "your file" : "your files"}; you can ask when {reading === 1 ? "it is" : "they are"} attached.
        </p>
      )}
      {fileNote && (
        <p className="px-2 pt-1 text-[12px] text-muted" role="status" data-testid="attach-note">
          {fileNote}
        </p>
      )}
      {queued && (
        <p className="flex items-center gap-2 px-2 pt-1.5 text-[12px] text-muted" role="status" data-testid="ask-queued">
          <span className="min-w-0 truncate">Next, after this answer: {queued.q}</span>
          <button type="button" onClick={() => setQueued(null)} className="shrink-0 text-fg underline-offset-2 hover:underline cursor-pointer">
            Cancel
          </button>
        </p>
      )}
    </div>
  );
}
