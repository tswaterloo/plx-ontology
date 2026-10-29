"use client";

import { ArrowUp, Box, Building2, FileBox, Hash, Package, Square, Truck, User, X, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { suggest as fetchSuggest } from "@/lib/api";
import { cn } from "@/lib/cn";
import { typeLabel } from "@/lib/format";
import type { Suggestion } from "@/lib/types";

export interface Pin {
  object_id: string;
  type: string;
  label: string;
}

export const SUGGEST_DEBOUNCE_MS = 150;
const MIN_CHARS = 2;

const TYPE_ICON: Record<string, LucideIcon> = {
  PurchaseOrder: FileBox,
  SalesOrder: Package,
  Customer: Building2,
  Supplier: Truck,
  Vendor: Truck,
  Item: Box,
  Lot: Hash,
  Person: User,
  Employee: User,
};

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

export function AskBox({
  onSubmit,
  size = "lg",
  placeholder = "Ask anything about orders, suppliers, customers, lots…",
  autoFocus,
  disabled,
  busy,
  onStop,
  initial = "",
}: {
  onSubmit: (question: string, pins: Pin[]) => void;
  size?: "lg" | "md";
  placeholder?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  busy?: boolean;
  onStop?: () => void;
  initial?: string;
}) {
  const [value, setValue] = useState(initial);
  const [pins, setPins] = useState<Pin[]>([]);
  const [items, setItems] = useState<Suggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [frag, setFrag] = useState({ start: 0, end: 0, text: "" });
  const ref = useRef<HTMLTextAreaElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ctrl = useRef<AbortController | null>(null);
  const cache = useRef(new Map<string, Suggestion[]>());
  const listId = useId();

  // Auto-grow the textarea up to a cap.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
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

  const lookup = useCallback(
    (text: string, caret: number) => {
      const f = fragmentAt(text, caret);
      setFrag(f);
      if (timer.current) clearTimeout(timer.current);
      ctrl.current?.abort();
      const q = f.text.trim();
      if (q.length < MIN_CHARS) {
        close();
        setItems([]);
        return;
      }
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
        try {
          const res = await fetchSuggest(q, c.signal);
          if (c.signal.aborted) return;
          const list = Array.isArray(res.suggestions) ? res.suggestions.slice(0, 8) : [];
          cache.current.set(q.toLowerCase(), list);
          setItems(list);
          setOpen(list.length > 0);
          setActive(-1);
        } catch {
          // Suggestions are a convenience: a failed lookup just shows none.
          if (!c.signal.aborted) close();
        }
      }, SUGGEST_DEBOUNCE_MS);
    },
    [close],
  );

  function pick(s: Suggestion) {
    const next = value.slice(0, frag.start) + s.label + " " + value.slice(frag.end).replace(/^\s+/, "");
    setValue(next);
    setPins((p) => (p.some((x) => x.object_id === s.object_id) ? p : [...p, { object_id: s.object_id, type: s.type, label: s.label }]));
    close();
    setItems([]);
    const caret = frag.start + s.label.length + 1;
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
      if ((e.key === "Enter" || e.key === "Tab") && active >= 0) {
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
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  const big = size === "lg";
  return (
    <div className="relative w-full">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        className={cn(
          "group relative flex flex-col rounded-3xl border border-border-strong bg-surface shadow-card transition-shadow focus-within:border-accent/60 focus-within:shadow-[0_0_0_4px_var(--accent-soft)]",
          big ? "p-3" : "p-2",
        )}
      >
        {pins.length > 0 && (
          <div className="flex flex-wrap gap-1 px-2 pt-1" aria-label="Picked items">
            {pins.map((p) => (
              <span key={p.object_id} className="inline-flex items-center gap-1 rounded-md bg-accent-soft px-1.5 py-0.5 text-xs font-medium text-accent">
                {p.label}
                <button
                  type="button"
                  aria-label={`Remove ${p.label}`}
                  onClick={() => setPins(pins.filter((x) => x.object_id !== p.object_id))}
                  className="rounded hover:bg-accent/10 cursor-pointer"
                >
                  <X className="size-3" aria-hidden />
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            ref={ref}
            value={value}
            rows={1}
            autoFocus={autoFocus}
            disabled={disabled}
            placeholder={placeholder}
            aria-label="Ask a question"
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
              "max-h-[220px] min-h-[44px] flex-1 resize-none bg-transparent px-2 py-2.5 text-fg placeholder:text-muted focus:outline-none",
              big ? "text-[17px]" : "text-[15px]",
            )}
          />
          {busy && onStop ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop answering"
              className="mb-1 flex size-10 shrink-0 items-center justify-center rounded-full bg-fg text-bg transition-opacity hover:opacity-85 cursor-pointer"
            >
              <Square className="size-3.5 fill-current" aria-hidden />
            </button>
          ) : (
            <button
              type="submit"
              aria-label="Ask"
              disabled={!value.trim() || disabled || busy}
              className="mb-1 flex size-10 shrink-0 items-center justify-center rounded-full bg-accent text-accent-fg transition-opacity hover:opacity-90 disabled:opacity-30 cursor-pointer disabled:cursor-default"
            >
              <ArrowUp className="size-5" aria-hidden />
            </button>
          )}
        </div>
      </form>
      <ul
        id={listId}
        role="listbox"
        aria-label="Suggestions"
        hidden={!open || items.length === 0}
        className="absolute inset-x-0 top-full z-40 mt-2 max-h-80 overflow-auto rounded-2xl border border-border bg-surface p-1.5 shadow-card animate-fade-in"
        data-testid="suggestions"
      >
        {items.map((s, i) => {
          const Icon = TYPE_ICON[s.type] ?? Hash;
          return (
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
              className={cn(
                "flex cursor-pointer items-center gap-3 rounded-xl px-3 py-2 text-sm",
                i === active ? "bg-surface-2" : "",
              )}
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted">
                <Icon className="size-3.5" aria-hidden />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium text-fg">{s.label}</span>
                {s.sublabel && <span className="block truncate text-xs text-muted">{s.sublabel}</span>}
              </span>
              <span className="hidden shrink-0 text-xs text-muted sm:inline">{typeLabel(s.type)}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
