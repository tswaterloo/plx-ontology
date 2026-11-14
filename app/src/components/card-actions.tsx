"use client";

import { lazy, Suspense, useState, type ComponentProps } from "react";
import type { Preset } from "@/lib/actions";
import type { ActionResult, CardActionOffer } from "@/lib/types";
import { Button } from "./ui/button";

const LazyActionDialog = lazy(() => import("./action-dialog"));

/** Preview, edit and confirm one action. Its body (the form, checks and confirm) loads when
 * an action is first opened, so pages with action buttons do not carry it in their first load. */
export function ActionDialog(props: ComponentProps<typeof LazyActionDialog>) {
  return (
    <Suspense fallback={null}>
      <LazyActionDialog {...props} />
    </Suspense>
  );
}

/** The card's action buttons; one opens its preview dialog. */
export function CardActions({
  card,
  onDone,
  state,
  preset,
}: {
  card: { id: string; subject: { label: string }; actions?: CardActionOffer[] | null };
  onDone: (r: ActionResult) => void;
  state?: ActionResult | null;
  preset?: Preset;
}) {
  const [open, setOpen] = useState<CardActionOffer | null>(null);
  const offers = card.actions ?? [];
  if (!offers.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-1 gap-y-1.5" data-testid="card-actions">
      {offers.map((o) => (
        <Button
          key={o.kind}
          variant={o.kind === "send_email" ? "secondary" : "ghost"}
          size="sm"
          className={o.kind === "send_email" ? "h-7 rounded-md px-2.5 text-[12px]" : "h-7 rounded-md px-2 text-[12px] font-normal text-muted hover:text-fg"}
          onClick={() => setOpen(o)}
          data-testid={`action-${o.kind}`}
        >
          {o.label}
        </Button>
      ))}
      {open && (
        <ActionDialog
          card={card}
          offer={open}
          state={state}
          // a reply keeps its thread's recipients: only the lines asking about a record
          preset={open.kind === "send_email" ? preset : open.kind === "reply_in_thread" && preset ? { append: preset.append } : undefined}
          onClose={() => setOpen(null)}
          onDone={(r) => {
            setOpen(null);
            onDone(r);
          }}
        />
      )}
    </div>
  );
}
