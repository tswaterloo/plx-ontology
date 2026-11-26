"use client";

import { Check, RotateCcw, SlidersHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";
import type { HomeSectionId } from "./home-copy";
import { SECTION_LABELS, SECTION_ORDER, type HomeDensity, type HomePrefs } from "./home-prefs";

export function HomeCustomizePopover({
  prefs,
  sectionCounts,
  isCustomized,
  onDensityChange,
  onToggleSection,
  onReset,
}: {
  prefs: HomePrefs;
  sectionCounts: Record<HomeSectionId, number>;
  isCustomized: boolean;
  onDensityChange: (density: HomeDensity) => void;
  onToggleSection: (id: HomeSectionId) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open]);

  const hiddenCount = SECTION_ORDER.filter((id) => !prefs.visibleSections[id]).length;

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
        data-testid="home-customize-trigger"
        className={cn(
          "inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[12px] font-medium transition-[background-color,border-color,color,transform] duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 active:scale-[0.98] cursor-pointer",
          open || hiddenCount > 0 || prefs.density === "compact"
            ? "border-border-strong bg-surface text-fg"
            : "border-border bg-transparent text-muted hover:border-border-strong hover:bg-hover hover:text-fg",
        )}
      >
        <SlidersHorizontal className="size-3.5" aria-hidden />
        <span>Customize</span>
        {hiddenCount > 0 && (
          <span className="num rounded-xs bg-surface-2 px-1 text-[10.5px] font-semibold text-fg">
            {SECTION_ORDER.length - hiddenCount}/{SECTION_ORDER.length}
          </span>
        )}
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Customize Home view"
          data-testid="home-customize-popover"
          className="absolute right-0 z-30 mt-1.5 w-[264px] rounded-lg border border-border-strong bg-raised p-3 shadow-pop"
        >
          <div className="flex items-center justify-between gap-2 pb-2.5 border-b border-border">
            <span className="text-[12.5px] font-semibold text-fg">Display</span>
            {isCustomized && (
              <button
                type="button"
                onClick={onReset}
                data-testid="home-customize-reset"
                className="inline-flex items-center gap-1 rounded-xs px-1.5 py-0.5 text-[11.5px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
              >
                <RotateCcw className="size-3" aria-hidden />
                Reset
              </button>
            )}
          </div>

          <div className="py-2.5 border-b border-border">
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-[0.04em] text-faint">Row density</p>
            <div className="grid grid-cols-2 gap-1.5">
              {(
                [
                  { id: "comfortable", label: "Comfortable" },
                  { id: "compact", label: "Compact" },
                ] as const
              ).map(({ id, label }) => {
                const active = prefs.density === id;
                return (
                  <button
                    key={id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => onDensityChange(id)}
                    data-testid={`popover-density-${id}`}
                    className={cn(
                      "flex h-7 items-center justify-center rounded-md border text-[12px] font-medium transition-colors cursor-pointer",
                      active
                        ? "border-border-strong bg-surface-2 text-fg"
                        : "border-border bg-transparent text-muted hover:border-border-strong hover:text-fg",
                    )}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="pt-2.5">
            <p className="mb-1.5 text-[11px] font-medium uppercase tracking-[0.04em] text-faint">Sections</p>
            <ul className="space-y-1">
              {SECTION_ORDER.map((id) => {
                const checked = prefs.visibleSections[id];
                const count = sectionCounts[id] ?? 0;
                return (
                  <li key={id}>
                    <button
                      type="button"
                      role="checkbox"
                      aria-checked={checked}
                      onClick={() => onToggleSection(id)}
                      data-testid={`toggle-section-${id}`}
                      className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition-colors hover:bg-hover cursor-pointer"
                    >
                      <span className="flex items-center gap-2 min-w-0">
                        <span
                          className={cn(
                            "flex size-4 shrink-0 items-center justify-center rounded-xs border transition-colors",
                            checked
                              ? "border-fg/80 bg-fg text-bg"
                              : "border-border-strong bg-bg text-transparent",
                          )}
                          aria-hidden
                        >
                          <Check className="size-3" strokeWidth={2.5} />
                        </span>
                        <span className={cn("truncate", checked ? "font-medium text-fg" : "text-muted")}>
                          {SECTION_LABELS[id]}
                        </span>
                      </span>
                      <span className="num shrink-0 text-[11.5px] text-faint">{count}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
