"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/cn";

/**
 * A right-hand sheet that floats a little off the window's edge on a desktop and fills the
 * screen on a phone, over a light scrim. Esc and the close button close it and return focus
 * to what opened it. `heading` replaces the drawn title (the accessible name stays `title`);
 * `aside` sits beside the close button; `footer` is pinned under the scrolling body.
 */
export function Sheet({
  open,
  onOpenChange,
  title,
  heading,
  icon,
  aside,
  description,
  footer,
  children,
  className,
  bodyClassName,
  trigger,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  heading?: React.ReactNode;
  icon?: React.ReactNode;
  aside?: React.ReactNode;
  description?: string;
  footer?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  trigger?: React.ReactNode;
}) {
  // what opened the drawer gets focus back when it closes (a chip, a step, a source line)
  const opener = React.useRef<HTMLElement | null>(null);
  if (open && !opener.current && typeof document !== "undefined") opener.current = document.activeElement as HTMLElement | null;
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      {trigger && <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>}
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(0_0_0/0.16)] animate-fade-in dark:bg-[rgb(0_0_0/0.45)]" />
        <Dialog.Content
          className={cn(
            "fixed inset-0 z-50 flex flex-col overflow-hidden bg-raised shadow-pop animate-fade-in focus:outline-none",
            "sm:inset-y-2 sm:right-2 sm:left-auto sm:w-[min(540px,calc(100vw-16px))] sm:rounded-xl",
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
          <div className="flex h-[52px] shrink-0 items-center gap-2.5 border-b border-border pr-2.5 pl-5">
            {icon}
            <Dialog.Title className={cn("min-w-0 flex-1 truncate text-[13px] font-medium", heading !== undefined && "sr-only")}>{title}</Dialog.Title>
            {heading !== undefined && <div className="min-w-0 flex-1 truncate text-[13px] font-medium" aria-hidden>{heading}</div>}
            {aside && <div className="flex shrink-0 items-center gap-0.5">{aside}</div>}
            <Dialog.Close
              aria-label="Close"
              className="flex size-8 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg cursor-pointer"
            >
              <X className="size-[18px]" strokeWidth={1.8} aria-hidden />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">{description ?? title}</Dialog.Description>
          <div className={cn("min-h-0 flex-1 overflow-y-auto px-5 pt-5 pb-8", bodyClassName)}>{children}</div>
          {footer && <div className="shrink-0 border-t border-border px-5 py-3 pb-[max(12px,env(safe-area-inset-bottom))]">{footer}</div>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
