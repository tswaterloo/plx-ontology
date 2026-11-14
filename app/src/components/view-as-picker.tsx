"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { getViewAsPeople } from "@/lib/api";
import { plainError } from "@/lib/errors";
import type { ViewAsPeople } from "@/lib/types";
import { switchTo } from "./view-as";

/** The owner's picker: everyone who may be viewed, filtered as you type. */
export default function ViewAsPicker({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [list, setList] = useState<ViewAsPeople | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  useEffect(() => {
    if (!open) return;
    const c = new AbortController();
    setError(null);
    getViewAsPeople(c.signal).then(setList, (e) => !c.signal.aborted && setError(plainError(e)));
    return () => c.abort();
  }, [open]);
  const people = (list?.people ?? []).filter((p) => p.name.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-[rgb(0_0_0/0.14)] dark:bg-[rgb(0_0_0/0.45)]" />
        <Dialog.Content
          className="fixed top-[max(12px,12vh)] left-1/2 z-50 flex max-h-[min(520px,calc(100dvh-24px))] w-[min(440px,calc(100vw-24px))] -translate-x-1/2 flex-col overflow-hidden rounded-[3px] bg-raised shadow-pop focus:outline-none"
          data-testid="view-as-picker"
        >
          <div className="border-b border-border px-4 py-3">
            <Dialog.Title className="text-[14px] font-semibold">View as someone</Dialog.Title>
            <Dialog.Description className="mt-0.5 text-[12px] leading-4 text-muted">
              Their My work, alerts and drafts, exactly as they see them. Read only; each view is audited.
            </Dialog.Description>
          </div>
          <label className="flex items-center gap-2 border-b border-border px-4 py-2">
            <Search className="size-4 text-muted" aria-hidden />
            <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Name" aria-label="Find a person" className="w-full bg-transparent text-[13px] outline-none" />
          </label>
          <ul className="min-h-0 flex-1 overflow-y-auto py-1">
            {error ? (
              <li className="px-4 py-2 text-[13px] text-muted">{error}</li>
            ) : !list ? (
              <li className="px-4 py-2 text-[13px] text-muted">Reading the people you can view as</li>
            ) : !list.available ? (
              <li className="px-4 py-2 text-[13px] text-muted">This server cannot view as anyone: it has no employee list or people exclusion loaded.</li>
            ) : people.length === 0 ? (
              <li className="px-4 py-2 text-[13px] text-muted">No one by that name.</li>
            ) : (
              people.map((p) => (
                <li key={p.key}>
                  <button type="button" onClick={() => switchTo({ key: p.key, name: p.name })} className="flex w-full items-center px-4 py-1.5 text-left text-[13px] hover:bg-hover cursor-pointer">
                    {p.name}
                  </button>
                </li>
              ))
            )}
          </ul>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
