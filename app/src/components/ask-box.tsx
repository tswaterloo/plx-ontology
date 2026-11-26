"use client";

import { ArrowUp, Box, Brain, Building2, Clock, CornerDownRight, ExternalLink, Factory, FileBox, FileSpreadsheet, FileText, FlaskConical, Hash, Mail, MapPin, Package, Paperclip, Percent, Plus, ShieldAlert, Slash, Square, TrendingUp, Truck, User, X, type LucideIcon } from "lucide-react";
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

export interface AskSkill {
  id: string;
  command: string;
  label: string;
  description: string;
  prompt: string;
  icon: LucideIcon;
}

export const ASK_SKILLS: readonly AskSkill[] = [
  {
    id: "late-pos",
    command: "/late-pos",
    label: "Late POs",
    description: "Late purchase orders by supplier with ETAs",
    prompt: "List all late purchase orders grouped by supplier with promised ETAs and days overdue.",
    icon: Clock,
  },
  {
    id: "qc-holds",
    command: "/qc-holds",
    label: "QC holds",
    description: "Open quality holds and affected customer orders",
    prompt: "Show all open quality holds, affected batches, and impacted customer orders.",
    icon: ShieldAlert,
  },
  {
    id: "revenue-why",
    command: "/revenue-why",
    label: "Revenue drivers",
    description: "Explain monthly revenue change by customer and product",
    prompt: "Explain the monthly revenue change this quarter broken down by customer and product.",
    icon: TrendingUp,
  },
  {
    id: "customer-brief",
    command: "/customer-brief",
    label: "Customer brief",
    description: "Full account brief with open orders, balance, and recent mail",
    prompt: "Prepare a full account brief with open orders, AR balance, and recent email for ",
    icon: Building2,
  },
  {
    id: "excel-tracker",
    command: "/excel-tracker",
    label: "Excel tracker",
    description: "Build a downloadable Excel workbook with formulas",
    prompt: "Build a downloadable Excel workbook with summary formulas for ",
    icon: FileSpreadsheet,
  },
  {
    id: "margin-check",
    command: "/margin-check",
    label: "Margin check",
    description: "Flag orders or SKUs with low or negative margin",
    prompt: "Flag open customer orders and SKUs with low or negative gross margin.",
    icon: Percent,
  },
  {
    id: "supplier-chase",
    command: "/supplier-chase",
    label: "Supplier chase",
    description: "Draft supplier follow-ups for overdue POs",
    prompt: "Draft concise supplier follow-up emails for overdue purchase orders with no confirmed ETA.",
    icon: Mail,
  },
  {
    id: "sharepoint-doc",
    command: "/sharepoint-doc",
    label: "SharePoint doc",
    description: "Create a document and prepare SharePoint upload",
    prompt: "Create a structured Word summary document and prepare it for SharePoint upload about ",
    icon: FileText,
  },
];

/** The slash skill being typed: text after a "/" (at the start or after whitespace) up to the caret. */
export function slashAt(text: string, caret: number): { start: number; end: number; query: string } | null {
  const before = text.slice(0, caret);
  const m = before.match(/(?:^|\s)\/([a-z0-9-]*)$/i);
  if (!m) return null;
  const start = caret - m[1].length - 1;
  const tail = text.slice(caret).match(/^[a-z0-9-]*/i);
  return { start, end: caret + (tail ? tail[0].length : 0), query: m[1].toLowerCase() };
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
  onQueue,
  onSteer,
  onDraftChange,
  size = "lg",
  placeholder = "Ask a question, @ for objects, / for skills...",
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
  /** Called on Enter / Queue while busy so the parent thread renders the follow-up in the stream. */
  onQueue?: (question: string, pins: Pin[], files: Attachment[], opts?: { deep?: boolean }) => void;
  /** Called on Cmd/Ctrl+Enter or Steer now while busy to stop the active run and submit at once. */
  onSteer?: (question: string, pins: Pin[], files: Attachment[], opts?: { deep?: boolean }) => void;
  /** Optional listener receiving the live unsent draft text (used by AskStart for quick-jump search). */
  onDraftChange?: (value: string) => void;
  /** Shows the "Deep research" toggle: the question is then asked with the deeper read. */
  deepable?: boolean;
  /** Files and images can be attached (a button, or pasted into the box). */
  attachable?: boolean;
  /** Kept for backwards caller compatibility; space switching now lives in the header SpaceSwitcher. */
  spaceLabel?: string;
  size?: "lg" | "md" | "sm";
  /** An id for the text field (the top bar's search is focused by Cmd+K / "/"). */
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
  const [slashItems, setSlashItems] = useState<readonly AskSkill[]>([]);
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashActive, setSlashActive] = useState(0);
  const [slashFrag, setSlashFrag] = useState({ start: 0, end: 0, query: "" });
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
  const slashListId = useId();

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
    onDraftChange?.(value);
  }, [draftKey, value, onDraftChange]);

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
    setSlashOpen(false);
    setSlashActive(0);
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
      if (!f) {
        setOpen(false);
        setActive(-1);
        setItems([]);
        const sl = slashAt(text, caret);
        if (sl) {
          const matched = ASK_SKILLS.filter(
            (s) =>
              !sl.query ||
              s.id.includes(sl.query) ||
              s.command.toLowerCase().includes(sl.query) ||
              s.label.toLowerCase().includes(sl.query) ||
              s.description.toLowerCase().includes(sl.query),
          );
          place();
          setSlashFrag(sl);
          setSlashItems(matched);
          setSlashOpen(matched.length > 0);
          setSlashActive(0);
        } else {
          setSlashOpen(false);
          setSlashItems([]);
        }
        return;
      }
      setSlashOpen(false);
      setSlashItems([]);
      setFrag(f);
      const q = f.text.trim();
      // numbers from the first digit ("1" of "po 1"), words from two letters
      const min = /\d$/.test(q) ? 1 : MIN_CHARS;
      if (q.length < min) {
        setOpen(false);
        setActive(-1);
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
    [close, place],
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

  /** Insert a workflow skill prompt in place of the typed `/command`. */
  function pickSkill(skill: AskSkill) {
    const span = slashFrag;
    const tail = value.slice(span.end).replace(/^\s+/, "");
    const next = value.slice(0, span.start) + skill.prompt + (tail ? ` ${tail}` : "");
    setValue(next);
    close();
    setSlashItems([]);
    const caret = span.start + skill.prompt.length;
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(caret, caret);
    });
  }

  /** Open the `/` skills menu from the toolbar button. */
  function openSkillsPicker() {
    if (slashOpen) {
      close();
      return;
    }
    const base = value;
    const needsSpace = base.length > 0 && !/\s$/.test(base);
    const next = base + (needsSpace ? " /" : "/");
    setValue(next);
    place();
    const start = base.length + (needsSpace ? 1 : 0);
    setSlashFrag({ start, end: next.length, query: "" });
    setSlashItems(ASK_SKILLS);
    setSlashOpen(true);
    setSlashActive(0);
    setOpen(false);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(next.length, next.length);
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

  function clearComposer() {
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

  function submit(intent: "auto" | "queue" | "steer" = "auto") {
    // a question with only files asks what they show
    const q = value.trim() || (files.length ? "What does this show?" : "");
    if (!q || disabled || reading > 0) return;
    const live = pins.filter((p) => q.includes(p.label));
    const useDeep = deepable && deep;

    if (busy) {
      if (intent === "steer") {
        if (onSteer) {
          if (useDeep) onSteer(q, live, files, { deep: true });
          else onSteer(q, live, files);
        } else {
          onStop?.();
          if (useDeep) onSubmit(q, live, files, { deep: true });
          else onSubmit(q, live, files);
        }
      } else if (onQueue) {
        if (useDeep) onQueue(q, live, files, { deep: true });
        else onQueue(q, live, files);
      } else {
        setQueued({ q, pins: live, files, deep: useDeep });
      }
    } else if (useDeep) {
      onSubmit(q, live, files, { deep: true });
    } else {
      onSubmit(q, live, files);
    }
    clearComposer();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (slashOpen && slashItems.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashActive((i) => (i + 1) % slashItems.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashActive((i) => (i <= 0 ? slashItems.length - 1 : i - 1));
        return;
      }
      if ((e.key === "Enter" || e.key === "Tab") && slashActive >= 0 && slashItems[slashActive]) {
        e.preventDefault();
        pickSkill(slashItems[slashActive]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
    }
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
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit(busy ? "steer" : "auto");
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit(busy ? "queue" : "auto");
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
  const hasDraft = Boolean(value.trim() || files.length > 0);
  const canSend = hasDraft && !disabled && reading === 0;
  const pop = "absolute inset-x-0 z-40 rounded-xl border border-border-strong bg-raised shadow-pop";

  const actionControls = (() => {
    if (busy) {
      if (!hasDraft) {
        return onStop ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Stop"
            title="Stop (Esc)"
            className={cn(
              "inline-flex shrink-0 items-center justify-center rounded-full bg-fg text-bg transition-all duration-150 hover:opacity-90 active:scale-[0.97] cursor-pointer",
              sm ? "m-0.5 size-[30px]" : "size-8",
            )}
          >
            <Square className="size-3 fill-current" aria-hidden />
          </button>
        ) : (
          <button
            type="submit"
            aria-label="Ask"
            title="Ask (Enter)"
            disabled
            aria-disabled
            className={cn(
              "inline-flex shrink-0 items-center justify-center rounded-full bg-surface-2 text-faint cursor-default",
              sm ? "m-0.5 size-[30px]" : "size-8",
            )}
          >
            <ArrowUp className="size-4" strokeWidth={2.4} aria-hidden />
          </button>
        );
      }
      return (
        <div className="flex shrink-0 items-center gap-1.5" data-testid="busy-composer-actions">
          {onStop && (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop"
              title="Stop current answer (Esc)"
              className={cn(
                "inline-flex shrink-0 items-center justify-center gap-1 rounded-lg border border-border bg-bg/80 text-[12px] font-medium text-muted transition-all duration-150 hover:border-border-strong hover:bg-surface-2 hover:text-fg active:scale-[0.98] cursor-pointer",
                sm ? "size-[30px]" : "h-8 px-2",
              )}
            >
              <Square className="size-2.5 fill-current" aria-hidden />
              {!sm && <span className="max-sm:hidden">Stop</span>}
            </button>
          )}
          <button
            type="submit"
            disabled={!canSend}
            aria-disabled={!canSend}
            title="Queue follow-up to run next (Enter)"
            data-testid="ask-queue-btn"
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border-strong bg-surface-2/85 px-2.5 text-[12px] font-medium text-fg transition-all duration-150 hover:bg-surface-2 active:scale-[0.98] disabled:opacity-50 cursor-pointer disabled:cursor-default",
              sm ? "h-[30px]" : "h-8",
            )}
          >
            <CornerDownRight className="size-3.5 shrink-0 text-muted" strokeWidth={2} aria-hidden />
            <span>Queue</span>
          </button>
          <button
            type="button"
            onClick={() => submit("steer")}
            disabled={!canSend}
            aria-disabled={!canSend}
            title="Stop current run and send now (Cmd+Enter)"
            data-testid="ask-steer-btn"
            className={cn(
              "inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-fg px-3 text-[12px] font-medium text-bg shadow-sm transition-all duration-150 hover:opacity-90 active:scale-[0.97] disabled:opacity-50 cursor-pointer disabled:cursor-default",
              sm ? "h-[30px]" : "h-8",
            )}
          >
            <ArrowUp className="size-3.5 shrink-0" strokeWidth={2.4} aria-hidden />
            <span>Steer now</span>
          </button>
        </div>
      );
    }

    return (
      <button
        type="submit"
        aria-label="Ask"
        title="Ask (Enter)"
        disabled={!canSend}
        aria-disabled={!canSend}
        className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-full bg-fg text-bg shadow-sm transition-all duration-150 hover:opacity-90 active:scale-[0.97] disabled:bg-surface-2 disabled:text-faint disabled:shadow-none cursor-pointer disabled:cursor-default",
          sm ? "m-0.5 size-[30px]" : "size-8",
        )}
      >
        <ArrowUp className="size-4" strokeWidth={2.4} aria-hidden />
      </button>
    );
  })();

  return (
    <div className="relative w-full" ref={boxRef}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(busy ? "queue" : "auto");
        }}
        data-testid="ask-composer-form"
        className={cn(
          "group relative flex flex-col border border-border-strong transition-all duration-150 focus-within:border-fg/30 focus-within:ring-2 focus-within:ring-fg/10",
          sm
            ? "rounded-xl bg-surface px-2 py-1"
            : "liquid-glass-surface rounded-2xl",
          big ? "px-4 pt-3.5 pb-3" : !sm && "px-3.5 pt-2.5 pb-2.5",
        )}
      >
        {attachable && files.length > 0 && (
          <ul className="flex flex-wrap gap-1.5 px-1 pt-0.5 pb-1.5" aria-label="Attachments" data-testid="attachments">
            {files.map((f) => (
              <li key={f.id} className="inline-flex h-6 items-center gap-1.5 rounded-lg border border-border bg-bg/80 pl-2 text-[12px] text-fg dark:bg-surface-2/80">
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
                  className="flex size-6 items-center justify-center rounded-md text-muted transition-colors duration-150 hover:text-fg active:scale-[0.95] cursor-pointer"
                >
                  <X className="size-3" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
        {pins.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-1 pt-0.5 pb-1.5" aria-label="Picked items">
            {pins.map((p) => {
              const Icon = typeIcon(p.type);
              return (
                <span key={p.object_id} className="inline-flex h-6 items-center gap-1.5 rounded-lg border border-border bg-bg/80 pl-2 text-[12px] text-fg dark:bg-surface-2/80">
                  <Icon className="size-3 text-muted" strokeWidth={2.2} aria-hidden />
                  {p.label}
                  <button
                    type="button"
                    aria-label={`Remove ${p.label}`}
                    onClick={() => setPins(pins.filter((x) => x.object_id !== p.object_id))}
                    className="flex size-6 items-center justify-center rounded-md text-muted transition-colors duration-150 hover:text-fg active:scale-[0.95] cursor-pointer"
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
            rows={big ? 2 : 1}
            autoFocus={autoFocus}
            disabled={disabled}
            placeholder={placeholder}
            aria-label={label}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={open || slashOpen}
            aria-controls={slashOpen ? slashListId : listId}
            aria-activedescendant={
              slashOpen && slashActive >= 0
                ? `${slashListId}-${slashActive}`
                : open && active >= 0
                  ? `${listId}-${active}`
                  : undefined
            }
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
              "max-h-[220px] flex-1 resize-none bg-transparent px-1 text-fg placeholder:text-faint outline-none focus:outline-none focus-visible:outline-none",
              sm ? "min-h-[36px] py-2 text-[14px] leading-5" : big ? "min-h-[92px] py-2 text-[15.5px] leading-6" : "min-h-[48px] py-1.5 text-[14.5px] leading-6",
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
                    disabled={disabled}
                    aria-label="Attach files or images"
                    title="Attach a PDF, spreadsheet or image (or paste one)"
                    className="m-0.5 flex size-[30px] shrink-0 items-center justify-center rounded-lg text-muted transition-all duration-150 hover:bg-surface-2 hover:text-fg active:scale-[0.97] cursor-pointer disabled:opacity-50"
                  >
                    <Paperclip className="size-4" strokeWidth={2} aria-hidden />
                  </button>
                </>
              )}
              {actionControls}
            </>
          ) : (
            <div className="flex items-center gap-1.5 border-t border-border/50 pt-2">
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
                    disabled={disabled}
                    aria-label="Attach files or images"
                    title="Attach a PDF, spreadsheet or image (or paste one)"
                    className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-transparent text-muted transition-all duration-150 hover:border-border hover:bg-surface-2 hover:text-fg active:scale-[0.97] cursor-pointer disabled:opacity-50"
                  >
                    <Plus className="size-4" strokeWidth={2} aria-hidden />
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
                  className={cn(
                    "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border px-2.5 text-[12px] font-medium transition-all duration-150 active:scale-[0.98] cursor-pointer",
                    deep
                      ? "border-fg/25 bg-fg text-bg shadow-sm"
                      : "border-transparent text-muted hover:border-border hover:bg-surface-2 hover:text-fg",
                  )}
                >
                  <Brain className="size-3.5" strokeWidth={2} aria-hidden />
                  <span>Deep research</span>
                </button>
              )}
              <button
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                  openSkillsPicker();
                }}
                disabled={disabled}
                aria-expanded={slashOpen}
                aria-label="Browse workflow skills"
                title="Workflow skills (/)"
                data-testid="skills-menu-btn"
                className={cn(
                  "inline-flex h-8 shrink-0 items-center gap-1 rounded-lg border px-2 text-[12px] font-medium transition-all duration-150 active:scale-[0.98] cursor-pointer disabled:opacity-50",
                  slashOpen
                    ? "border-border-strong bg-surface-2 text-fg"
                    : "border-transparent text-muted hover:border-border hover:bg-surface-2 hover:text-fg",
                )}
              >
                <Slash className="size-3.5" strokeWidth={2} aria-hidden />
                <span>Skills</span>
              </button>
              <span className="flex-1" />
              <span className="hidden select-none text-[11px] text-faint sm:inline" data-testid="composer-shortcut-hint">
                {busy ? "Enter to queue · Cmd+Enter to steer" : "Enter to send"}
              </span>
              {actionControls}
            </div>
          )}
        </div>
      </form>
      <ul
        id={slashListId}
        role="listbox"
        aria-label="Workflow skills"
        hidden={!slashOpen || slashItems.length === 0}
        className={cn(pop, "max-h-80 overflow-auto p-1.5 plx-panel-animate", up ? "bottom-full mb-2" : "top-full mt-2")}
        data-testid="skills-menu"
        data-placement={up ? "up" : "down"}
      >
        <li role="presentation" className="px-2 pt-1 pb-1.5 text-[11.5px] font-semibold tracking-[0.02em] text-faint">
          Workflow skills
        </li>
        {slashItems.map((skill, i) => {
          const Icon = skill.icon;
          return (
            <li
              key={skill.id}
              id={`${slashListId}-${i}`}
              role="option"
              aria-selected={i === slashActive}
              onMouseDown={(e) => {
                e.preventDefault();
                pickSkill(skill);
              }}
              onMouseEnter={() => setSlashActive(i)}
              className={cn(
                "flex min-h-10 cursor-pointer items-center gap-2.5 rounded-lg px-2 py-1.5 text-[13px] transition-colors duration-100",
                i === slashActive ? "bg-row-on text-fg" : "text-fg hover:bg-hover",
              )}
              data-testid={`skill-option-${skill.id}`}
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-surface text-accent" aria-hidden>
                <Icon className="size-3.5" strokeWidth={2} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="font-mono text-[12px] font-medium text-accent">{skill.command}</span>
                  <span className="truncate text-[12.5px] font-medium text-fg">{skill.label}</span>
                </span>
                <span className="block truncate text-[12px] leading-4 text-muted">{skill.description}</span>
              </span>
            </li>
          );
        })}
      </ul>
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
          <button
            type="button"
            onClick={() => {
              const next = queued;
              setQueued(null);
              if (onSteer) {
                if (next.deep) onSteer(next.q, next.pins, next.files, { deep: true });
                else onSteer(next.q, next.pins, next.files);
              } else {
                onStop?.();
                if (next.deep) onSubmit(next.q, next.pins, next.files, { deep: true });
                else onSubmit(next.q, next.pins, next.files);
              }
            }}
            className="shrink-0 font-medium text-fg underline decoration-border-strong underline-offset-2 transition-colors hover:decoration-fg cursor-pointer"
          >
            Send now
          </button>
          <button type="button" onClick={() => setQueued(null)} className="shrink-0 text-muted transition-colors hover:text-fg cursor-pointer">
            Cancel
          </button>
        </p>
      )}
    </div>
  );
}
