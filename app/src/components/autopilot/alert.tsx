"use client";

// Something that happened and waits on the person but has nothing to prepare (an approval
// asked of them, a QC hold, a Mission Control question): what happened, why, where it is
// decided and its records. It is decided in its own system; here it is only set aside, or
// passed to a colleague (on their list too, under their own access).

import { useEffect, useState } from "react";
import { ItemActions } from "@/components/autopilot/item-actions";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { getAutopilotColleagues, handAutopilotItem, setFollowing, type PreparedItem } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/cn";

export function AlertCard({ item, linked, onGone }: { item: PreparedItem; linked?: boolean; onGone: (id: string) => void }) {
  const [passing, setPassing] = useState(false);
  return (
    <li id={`prepared-${item.item_id}`} className={cn("space-y-2 py-4", linked && "border-l-2 border-accent pl-3")} data-testid="prepared-alert">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="text-[13px] font-medium">{item.subject || "From the records"}</span>
        <span className="num ml-auto text-[12px] text-muted">{formatDateTime(item.created_at)}</span>
      </div>
      <p className="max-w-[46rem] text-[13px]">{item.summary}</p>
      {item.message && <p className="max-w-[46rem] whitespace-pre-wrap text-[12px] text-muted">{item.message}</p>}
      {item.facts.length > 0 && (
        <ul className="max-w-[46rem] list-disc space-y-0.5 pl-4 text-[12px] text-muted">
          {item.facts.map((f, n) => (
            <li key={n}>{f}</li>
          ))}
        </ul>
      )}
      {item.note && <p className="text-[12px] text-muted">{item.note}</p>}
      {(item.links ?? []).length > 0 && (
        <div className="flex flex-wrap gap-3 text-[13px]">
          {(item.links ?? []).map((l) => (
            <span key={l.href} className="inline-flex items-baseline gap-2">
              <a href={l.href} {...(l.href.startsWith("/") ? {} : { target: "_blank", rel: "noopener noreferrer" })} className="text-accent underline-offset-2 hover:underline">
                {l.label}
              </a>
              {l.href.startsWith("/object/") && <Follow objectId={decodeURIComponent(l.href.slice("/object/".length))} label={l.label} />}
            </span>
          ))}
        </div>
      )}
      {item.handed_from && <p className="text-[12px] text-muted" data-testid="alert-handed-from">Passed to you by {item.handed_from}. Setting it aside clears it for them too.</p>}
      <ItemActions item={item} onGone={onGone} setAsideOnly={!!item.handed_from} entries={item.handed_from ? [] : [{ label: "Pass to a colleague", run: () => setPassing(true) }]} />
      <PassOn itemId={item.item_id} handedTo={item.handed_to} open={passing} onClose={() => setPassing(false)} />
    </li>
  );
}

// Follow a record the alert is about: told when it or a record linked to it changes (the
// watch's own follow, which checks the record opens for you); nothing is decided here.
function Follow({ objectId, label }: { objectId: string; label: string }) {
  const [state, setState] = useState<"idle" | "busy" | "on" | "error">("idle");
  if (state === "on") return <span className="text-[12px] text-muted">Following</span>;
  return (
    <button
      type="button"
      className="text-[12px] text-muted underline-offset-2 hover:underline disabled:opacity-50"
      aria-label={`Follow ${label}`}
      disabled={state === "busy"}
      onClick={() => {
        setState("busy");
        setFollowing(objectId, true).then(
          () => setState("on"),
          () => setState("error"),
        );
      }}
    >
      {state === "error" ? "Could not follow" : "Follow"}
    </button>
  );
}

function PassOn({ itemId, handedTo, open, onClose }: { itemId: string; handedTo?: string; open: boolean; onClose: () => void }) {
  const [people, setPeople] = useState<{ key: string; name: string }[] | null>(null);
  const [pick, setPick] = useState("");
  const [to, setTo] = useState(handedTo ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (act: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await act();
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    if (!open || people !== null) return;
    void run(async () => {
      const got = (await getAutopilotColleagues()).people ?? [];
      setPeople(got);
      setPick(got[0]?.key ?? "");
    });
  }, [open, people]);
  const hand = () =>
    run(async () => {
      await handAutopilotItem(itemId, pick);
      setTo(people?.find((p) => p.key === pick)?.name ?? "a colleague");
      onClose();
    });
  return (
    <div className="space-y-1">
      {to && <p className="text-[12px] text-muted" data-testid="alert-handed-to">Passed to {to}. If they set it aside, it leaves your list too.</p>}
      {open && people !== null && (people.length === 0 ? (
        <p className="text-[12px] text-muted">Nobody to pass it to.</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <select className="h-8 border border-border bg-transparent px-2 text-[13px]" aria-label="Colleague" value={pick} onChange={(e) => setPick(e.target.value)}>
            {people.map((p) => (
              <option key={p.key} value={p.key}>
                {p.name}
              </option>
            ))}
          </select>
          <Button size="sm" disabled={busy || !pick} onClick={() => void hand()}>
            Pass on
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
        </div>
      ))}
      {error && <ErrorNotice message={error} />}
    </div>
  );
}
