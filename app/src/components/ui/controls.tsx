"use client";

import * as React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";

/**
 * Custom-styled select control with a crisp hairline border, elevated white surface,
 * and subtle chevron so pages never fall back to thick-bordered native HTML selects.
 */
export interface SelectOption {
  value: string;
  label: string;
}

export function StyledSelect({
  value,
  onChange,
  options,
  ariaLabel,
  className,
  size = "sm",
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  ariaLabel: string;
  className?: string;
  size?: "sm" | "md";
  disabled?: boolean;
}) {
  return (
    <label
      className={cn(
        "relative inline-flex items-center rounded-md border border-border bg-transparent transition-colors hover:border-border-strong hover:bg-hover focus-within:border-accent/70 focus-within:ring-1 focus-within:ring-ring/30",
        size === "sm" ? "h-7" : "h-8",
        disabled && "opacity-50 pointer-events-none",
        className,
      )}
    >
      <select
        aria-label={ariaLabel}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          "h-full w-full cursor-pointer appearance-none bg-transparent pr-7 pl-2.5 font-medium text-fg outline-none",
          size === "sm" ? "text-[12px]" : "text-[13px]",
        )}
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value} className="bg-bg text-fg">
            {opt.label}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 size-3.5 text-muted" strokeWidth={2} aria-hidden />
    </label>
  );
}

/**
 * Clean segmented control for switching compact views or filter modes.
 */
export function SegmentedControl<T extends string>({
  value,
  onChange,
  items,
  ariaLabel,
  className,
}: {
  value: T;
  onChange: (value: T) => void;
  items: { value: T; label: string; count?: number }[];
  ariaLabel: string;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className={cn("inline-flex items-center gap-0.5 rounded-md border border-border bg-surface/60 p-0.5", className)}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(item.value)}
            className={cn(
              "inline-flex h-6 items-center gap-1.5 rounded-sm px-2.5 text-[12px] transition-colors cursor-pointer",
              active ? "bg-bg font-medium text-fg" : "text-muted hover:text-fg",
            )}
          >
            <span>{item.label}</span>
            {typeof item.count === "number" && (
              <span className={cn("num text-[11px]", active ? "text-muted" : "text-faint")}>{item.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
