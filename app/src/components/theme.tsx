"use client";

import { ThemeProvider as NextThemes, useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";

export function ThemeProvider({ children, nonce }: { children: React.ReactNode; nonce?: string }) {
  return (
    <NextThemes attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange nonce={nonce}>
      {children}
    </NextThemes>
  );
}

const OPTIONS = [
  { value: "light", label: "Light theme", text: "Light" },
  { value: "dark", label: "Dark theme", text: "Dark" },
  { value: "system", label: "Match system theme", text: "Auto" },
] as const;

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return (
    <div role="radiogroup" aria-label="Theme" className={cn("inline-flex items-center gap-0.5 rounded-md border border-border bg-surface p-0.5 text-meta", className)}>
      {OPTIONS.map(({ value, label, text }) => {
        const on = mounted && theme === value;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={on}
            aria-label={label}
            onClick={() => setTheme(value)}
            className={cn(
              "rounded-sm px-2 py-0.5 transition-colors cursor-pointer",
              on ? "bg-bg font-medium text-fg shadow-xs dark:bg-surface-2" : "text-muted hover:text-fg",
            )}
          >
            {text}
          </button>
        );
      })}
    </div>
  );
}
