"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * A right-hand drawer (full screen below 640 px) over a 40% scrim. Esc and the close
 * button close it and return focus to what opened it (Radix restores focus to the trigger).
 */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
  className,
  trigger,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  children: React.ReactNode;
  className?: string;
  trigger?: React.ReactNode;
}) {
  // what opened the drawer gets focus back when it closes (a chip, a step, a source line)
  const opener = React.useRef<HTMLElement | null>(null);
  if (open && !opener.current && typeof document !== "undefined") opener.current = document.activeElement as HTMLElement | null;
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>}
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(28_26_23/0.32)] animate-fade-in" />
        <Dialog.Content
          className={cn(
            "fixed inset-y-0 right-0 z-50 flex w-full flex-col border-l border-border-strong bg-bg shadow-pop animate-fade-in focus:outline-none sm:w-[520px]",
            className,
          )}
          data-testid="evidence-drawer"
          onCloseAutoFocus={(e) => {
            const el = opener.current;
            opener.current = null;
            if (el && el !== document.body && document.contains(el)) {
              e.preventDefault();
              el.focus();
            }
          }}
        >
          <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-3">
            <Dialog.Title className="truncate text-section">{title}</Dialog.Title>
            <Dialog.Close
              aria-label="Close"
              className="flex size-9 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-surface-2 hover:text-fg cursor-pointer"
            >
              <X className="size-5" aria-hidden />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">{description ?? title}</Dialog.Description>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
