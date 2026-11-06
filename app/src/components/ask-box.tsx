"use client";

import { ArrowUp, Box, Building2, Factory, FileBox, FlaskConical, Hash, MapPin, Package, Square, Truck, User, X, type LucideIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { Fragment, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { suggest as fetchSuggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plural, recordLabel, safeHref, typeLabel } from "@/lib/format";
import type { Suggestion } from "@/lib/types";
import { SystemTile } from "./ui/glyphs";

export interface Pin {
  object_id: string;
  type: string;
  label: string;
}

export const SUGGEST_DEBOUNCE_MS = 150;
const MIN_CHARS = 2;
/** Room the list needs below the box; with less, it opens upward. */
const DROP_ROOM = 360;
/** The longest phrase looked up: the words typed before the caret, at most this many. */
export const PHRASE_WORDS = 4;

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
 * record is the target, the other systems become dots), and, when the typed words name a
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
    // a different code under one name is another record, never a copy of the first
    const i = same.findIndex((r) => !facts || !r.facts || r.facts === facts);
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

/** The word being typed at the caret (letters, digits, - / # .), or "". */
export function fragmentAt(text: string, caret: number): { start: number; end: number; text: string } {
  const before = text.slice(0, caret);
  const after = text.slice(caret);
  const m = before.match(/[\p{L}\p{N}\-/#.&']+$/u);
  const n = after.match(/^[\p{L}\p{N}\-/#.&']*/u);
  const start = caret - (m ? m[0].length : 0);
  const end = caret + (n ? n[0].length : 0);
  return { start, end, text: text.slice(start, end) };
}

/** The words typed up to the caret within the current clause, at most `PHRASE_WORDS` of
 * them ("what did we tell three ships" -> "we tell three ships"), and the span they take;
 * the server looks up each trailing phrase of them, longest first. */
export function phraseAt(text: string, caret: number): { start: number; end: number; text: string } {
  const word = fragmentAt(text, caret);
  const before = text.slice(0, word.start);
  const clause = before.split(/[?!,;:()]/).pop() ?? "";
  const words = clause.match(/[\p{L}\p{N}\-/#.&']+\s+/gu) ?? [];
  const kept = words.slice(-(PHRASE_WORDS - 1));
  const head = kept.join("");
  const start = head ? before.lastIndexOf(head) : word.start;
  return { start: start < 0 ? word.start : start, end: word.end, text: text.slice(start < 0 ? word.start : start, word.end) };
}

const fold = (s: string) =>
  s
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();

/** The part of a typed phrase a picked suggestion's name stands for: its longest
 * trailing run of words found in the name ("tell three ships" + "Three Ships (NIU BODY
 * INC.)" -> "three ships"), else its last word. */
export function spanFor(phrase: { start: number; end: number; text: string }, label: string): { start: number; end: number } {
  const name = ` ${fold(label)} `;
  const parts = [...phrase.text.matchAll(/\S+/g)];
  for (let i = 0; i < parts.length; i++) {
    const tail = fold(parts.slice(i).map((p) => p[0]).join(" "));
    if (tail && name.includes(` ${tail}`)) return { start: phrase.start + (parts[i].index ?? 0), end: phrase.end };
  }
  const last = parts[parts.length - 1];
  return { start: phrase.start + (last?.index ?? 0), end: phrase.end };
}

export function AskBox({
  onSubmit,
  size = "lg",
  placeholder = "Question about a PO, SO, supplier or lot",
  autoFocus,
  disabled,
  busy,
  onStop,
  initial = "",
  dropUp = false,
  inputId,
  label = "Ask a question",
}: {
  onSubmit: (question: string, pins: Pin[]) => void;
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
}) {
  const router = useRouter();
  const [empty, setEmpty] = useState("");
  const [value, setValue] = useState(initial);
  const [pins, setPins] = useState<Pin[]>([]);
  const [items, setItems] = useState<(Suggestion & { systems?: string[]; facts?: string; shared?: number })[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [frag, setFrag] = useState({ start: 0, end: 0, text: "" });
  const [up, setUp] = useState(dropUp);
  const [searching, setSearching] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const ref = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  const cache = useRef(new Map<string, Suggestion[]>());
  const listId = useId();

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
      const f = phraseAt(text, caret);
      setFrag(f);
      setEmpty("");
      if (timer.current) clearTimeout(timer.current);
      ctrl.current?.abort();
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
        const lastWord = q.split(/\s+/).pop() ?? "";
        setEmpty(hit.length === 0 && lastWord.length >= 4 ? lastWord : "");
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
          // a word of four letters or more that names no record says so (then ask)
          const lastWord = q.split(/\s+/).pop() ?? "";
          setEmpty(list.length === 0 && lastWord.length >= 4 ? lastWord : "");
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
    const span = spanFor(frag, label);
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

  function submit() {
    const q = value.trim();
    if (!q || disabled || busy) return;
    const live = pins.filter((p) => q.includes(p.label));
    onSubmit(q, live);
    setValue("");
    setPins([]);
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
        openRecord(items[active]);
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

  const big = size === "lg";
  const sm = size === "sm";
  const pop = "absolute inset-x-0 z-40 rounded-xl bg-raised shadow-pop";
  return (
    <div className="relative w-full" ref={boxRef}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className={cn(
          "group relative flex flex-col bg-bg transition-shadow",
          sm
            ? "rounded-lg border border-control focus-within:border-fg"
            : "rounded-2xl shadow-[var(--lift),0_8px_24px_-12px_rgb(0_0_0/0.12)] focus-within:shadow-[var(--lift),0_0_0_1px_color-mix(in_srgb,var(--accent)_45%,transparent),0_0_0_4px_color-mix(in_srgb,var(--accent)_12%,transparent)]",
          big ? "p-2.5" : sm ? "p-0.5" : "p-2",
        )}
      >
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
        <div className="flex items-end gap-2">
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
              lookup(e.target.value, e.target.selectionStart ?? e.target.value.length);
            }}
            onKeyDown={onKeyDown}
            onBlur={() => setTimeout(close, 120)}
            className={cn(
              "max-h-[220px] flex-1 resize-none bg-transparent px-2 text-fg placeholder:text-faint focus:outline-none",
              sm ? "min-h-[34px] py-1.5 text-[13px]" : big ? "min-h-[48px] py-3 text-[15px] leading-6" : "min-h-[40px] py-2.5 text-[14px] leading-5",
            )}
          />
          {busy && onStop ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop answering"
              title="Stop (Esc)"
              className={cn("flex shrink-0 items-center justify-center rounded-lg bg-fg text-bg hover:opacity-85 cursor-pointer", sm ? "m-0.5 size-[30px]" : "mb-0.5 size-8")}
            >
              <Square className="size-3 fill-current" aria-hidden />
            </button>
          ) : (
            <button
              type="submit"
              aria-label="Ask"
              title="Ask (Enter)"
              disabled={!value.trim() || disabled || busy}
              aria-disabled={!value.trim() || disabled || busy}
              className={cn(
                "flex shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg shadow-sm hover:brightness-110 disabled:bg-surface-2 disabled:text-faint disabled:shadow-none cursor-pointer disabled:cursor-default",
                sm ? "m-0.5 size-[30px]" : "mb-0.5 size-8",
              )}
            >
              <ArrowUp className="size-4" strokeWidth={2.4} aria-hidden />
            </button>
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
                openRecord(s);
              }}
              onMouseEnter={() => setActive(i)}
              className={cn("flex min-h-11 cursor-pointer items-center gap-3 rounded-lg px-2 py-1.5 text-[13px]", i === active ? "bg-hover" : "")}
              data-type={s.type}
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-surface-2 text-muted" aria-hidden>
                <Icon className="size-3.5" strokeWidth={2} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-fg">{recordLabel(s.type, s.label)}</span>
                <span className="block truncate text-[12px] leading-4 text-muted">{sub}</span>
              </span>
              {(s.systems ?? []).length > 0 && (
                <span className="hidden shrink-0 items-center gap-1 sm:flex" aria-label={`Found in ${s.systems!.join(" and ")}`}>
                  {s.systems!.map((x) => (
                    <SystemTile key={x} system={x} size={16} />
                  ))}
                </span>
              )}
              <button
                type="button"
                tabIndex={-1}
                aria-label={`Ask about ${recordLabel(s.type, s.label)}`}
                title="Ask about this (Tab)"
                onMouseDown={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  pick(s);
                }}
                className={cn("inline-flex h-7 shrink-0 items-center rounded-md px-2 text-[12px] text-muted hover:bg-surface-2 hover:text-fg cursor-pointer", i === active ? "opacity-100" : "sm:opacity-0")}
                data-testid="ask-about"
              >
                Ask
              </button>
            </li>
            </Fragment>
          );
        })}
      </ul>
      {searching && !open && (
        <p className={cn(pop, "px-3.5 py-2.5 text-[12px] text-muted", up ? "bottom-full mb-2" : "top-full mt-2")} role="status">
          Searching…
        </p>
      )}
      {empty && !open && (
        <p className={cn(pop, "px-3.5 py-2.5 text-[12px] text-muted", up ? "bottom-full mb-2" : "top-full mt-2")} role="status" data-testid="no-record">
          No record is named “{empty}”. Press Enter to ask anyway.
        </p>
      )}
    </div>
  );
}
