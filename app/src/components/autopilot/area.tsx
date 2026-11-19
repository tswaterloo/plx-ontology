"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { EveryoneView, PreparedView, emptyWords } from "@/components/autopilot/prepared";
import { DraftsPanel } from "@/components/autopilot/opt-in";
import { TeamsConnect } from "@/components/autopilot/teams-connect";
import { ErrorNotice, LoadProblem } from "@/components/ui/notice";
import { Pending } from "@/components/ui/skeleton";
import { getAutopilotDrafts, type PreparedItem } from "@/lib/api";
import { cn } from "@/lib/cn";
import { noticeKind, plainError } from "@/lib/errors";
import type { AutopilotArea as Area } from "@/lib/types";

// The autopilot page: what to prepare for you (six switches, each yours to turn off even when the owner turned it on) and what was
// prepared, newest first, each to confirm, edit, copy or open in Outlook. Admins also see who
// switched what on and how each workflow is doing. Nothing is sent until you confirm it.

type Tab = "prepared" | "everyone";

export function AutopilotView({ area: initial }: { area: Area; onReload?: () => void }) {
  const [area, setArea] = useState(initial);
  useEffect(() => setArea(initial), [initial]);
  const [tab, setTab] = useState<Tab>("prepared");
  const [items, setItems] = useState<PreparedItem[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [round, setRound] = useState(0);
  const reload = useCallback(() => setRound((n) => n + 1), []);
  // links from Home, the work log and the desktop app: ?item=<id> opens that item,
  // ?tab=audit is the log (its own page under Admin)
  const params = useSearchParams();
  const router = useRouter();
  const linked = params.get("item");
  useEffect(() => {
    if (params.get("tab") === "audit") router.replace("/autopilot-log");
  }, [params, router]);
  useEffect(() => {
    if (!linked || !items) return;
    document.getElementById(`prepared-${linked}`)?.scrollIntoView({ block: "center" });
  }, [linked, items]);
  useEffect(() => {
    const ctl = new AbortController();
    getAutopilotDrafts(ctl.signal).then(
      (r) => {
        setItems(r.items);
        setError(null);
      },
      (e) => !ctl.signal.aborted && setError(e),
    );
    return () => ctl.abort();
  }, [round]);
  const admin = area.owner || area.drafts_switched_on != null;
  const tabs: [Tab, string][] = [["prepared", "Prepared for me"], ...(admin ? ([["everyone", "Everyone"]] as [Tab, string][]) : [])];
  return (
    <div className="space-y-4" data-testid="autopilot">
      {area.switches.global_off && <ErrorNotice tone="notice" message="Every autopilot write is switched off. A confirm is recorded and nothing is sent." />}
      {area.switches.autopilot_off && <ErrorNotice tone="notice" message="The autopilot is switched off on this server: nothing new is prepared." />}
      <DraftsPanel area={area} onArea={setArea} />
      <TeamsConnect />
      <nav className="flex h-9 items-stretch gap-4" role="tablist" aria-label="Autopilot">
        {tabs.map(([t, label]) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={cn("relative flex items-center text-[13px] cursor-pointer", tab === t ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:bg-accent" : "text-muted hover:text-fg")}
            onClick={() => setTab(t)}
          >
            {label}
            {t === "prepared" && items && items.length > 0 && <span className="num ml-1.5 text-[11px] text-faint">{items.length}</span>}
          </button>
        ))}
      </nav>
      <div role="tabpanel">
        {tab === "everyone" && admin ? (
          <EveryoneView area={area} />
        ) : error ? (
          <LoadProblem message={plainError(error)} busy={noticeKind(error) === "busy"} onRetry={reload} />
        ) : items ? (
          <>
            {linked && !items.some((i) => i.item_id === linked) && <p className="py-2 text-[13px] text-muted">That item is no longer waiting for you: it was answered, decided or set aside.</p>}
            <PreparedView items={items} empty={emptyWords(area)} linked={linked} onGone={(id) => setItems((xs) => (xs ?? []).filter((x) => x.item_id !== id))} onChanged={reload} />
          </>
        ) : (
          <Pending label="Reading what was prepared" />
        )}
      </div>
    </div>
  );
}
