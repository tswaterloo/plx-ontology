"use client";

// A change Ask prepared in a business system (or a document's text): nothing is done until
// the person previews it and confirms in two clicks. The confirm carries the preview's
// hash, so a change that moved since it was shown is refused and shown again.

import { useState } from "react";
import { getPrepared, preparedStep } from "@/lib/api";
import type { PreparedWrite } from "@/lib/types";

type Action = NonNullable<PreparedWrite["actions"]>[number];
import { errorWords } from "./upload-draft";
import { Button } from "./ui/button";

function shown(value: unknown): string {
  if (value === null || value === undefined || value === "") return "(empty)";
  return typeof value === "string" ? value : JSON.stringify(value);
}

const DONE: Record<string, string> = {
  done: "Done.",
  dry_run: "Recorded only: not written (see why above).",
  needs_confirm: "Not written: it needs your confirm that it goes live.",
  failed: "The system refused it. Nothing changed.",
  unknown: "The system did not answer. Check it before trying again.",
  refused: "Refused by the write checks. Nothing changed.",
  duplicate: "Already done once; not repeated.",
};

const SYSTEMS: Record<string, string> = {
  bc: "Business Central",
  mc: "Mission Control",
  planner: "Planner",
  calendar: "Calendar",
  sharepoint: "SharePoint",
  portal: "Portal",
  graph: "Microsoft 365",
};

function ids(target: unknown): string {
  if (!target || typeof target !== "object") return "";
  return Object.entries(target as Record<string, unknown>)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k} ${shown(v)}`)
    .join(", ");
}

// Every action exactly as it runs: system, operation, the record (its name as the system
// holds it, and its ids), every field it sends, a message whole.
// The BC company the request goes to: its id and BC's own name for it.
type Company = { id?: string | null; name?: string | null } | null | undefined;

function ActionShown({ action }: { action: Action }) {
  const message = action.message;
  const company = (action as { company?: Company }).company;
  const condition = ids(action.condition);
  return (
    <div className="mt-1 text-[12px] leading-4" data-testid="prepared-action">
      <p>
        <span className="font-medium">
          {SYSTEMS[action.system ?? ""] ?? action.system}
        </span>
        {" · "}
        {action.operation}
        {action.decision ? ` · ${action.decision}` : ""}
      </p>
      {action.system === "bc" && (
        <p className="text-muted" data-testid="prepared-company">
          Company:{" "}
          {company?.id
            ? `${company.name ?? "(name not checked)"} (${company.id})`
            : "(not known: nothing is written)"}
        </p>
      )}
      {(action.record || ids(action.target)) && (
        <p className="text-muted">
          {action.record ? `${action.record}` : "New record"}
          {ids(action.target) ? ` (${ids(action.target)})` : ""}
        </p>
      )}
      {message ? (
        <div className="mt-1 space-y-0.5" data-testid="prepared-message">
          <p>From: {shown(message.sender)}</p>
          <p>To: {(message.to ?? []).join(", ") || "(nobody)"}</p>
          {(message.cc ?? []).length > 0 && (
            <p>Cc: {(message.cc ?? []).join(", ")}</p>
          )}
          {message.subject !== null && message.subject !== undefined && (
            <p>Subject: {message.subject}</p>
          )}
          <p className="whitespace-pre-wrap border-l border-muted pl-2">
            {shown(message.body)}
          </p>
          {(message.attachments ?? []).map((a) => (
            <p key={a.file_id ?? a.name}>
              Attached:{" "}
              {a.file_id ? (
                <a className="text-accent hover:underline" href={`/api/instruct/files/${encodeURIComponent(a.file_id)}`} download={a.name ?? undefined}>
                  {a.name}
                </a>
              ) : (
                a.name
              )}
              {a.size ? ` (${Math.max(1, Math.round(a.size / 1024))} KB)` : ""}
            </p>
          ))}
        </div>
      ) : action.fields && typeof action.fields === "object" ? (
        <ul className="mt-0.5">
          {Object.entries(action.fields as Record<string, unknown>).map(
            ([k, v]) => (
              <li key={k}>
                {k}: {shown(v)}
              </li>
            ),
          )}
        </ul>
      ) : null}
      {condition && <p className="text-muted">Only if: {condition}</p>}
      {(action.people ?? []).length > 0 && !message && (
        <p className="text-muted">
          Reaches: {(action.people ?? []).join(", ")}
        </p>
      )}
    </div>
  );
}

const STATES: Record<string, string> = {
  dismissed: "Dismissed; nothing was written.",
  undone: "Undone.",
  expired:
    "Expired after a day. Nothing was written; prepare it again to check it against the system as it is now.",
  not_run:
    "Confirmed, but no result was recorded. Check the system before asking again.",
};

export function PreparedChange({ id }: { id: string }) {
  const [card, setCard] = useState<PreparedWrite | null>(null);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (step: () => Promise<PreparedWrite>) => {
    setBusy(true);
    setError(null);
    try {
      setCard(await step());
    } catch (err) {
      setError(errorWords(err));
    } finally {
      setBusy(false);
      setArmed(false);
    }
  };
  // a change prepared again is a new item: its steps go to the new id
  const cur = card?.prepared_id ?? id;
  const open = card?.state === "prepared" && !!card.preview_hash;
  const written = (card?.outcome ?? []).some((o) => o.status === "done");
  const outcomes = (card?.outcome ?? []).map((o) => o.status ?? "");
  const retry =
    card?.state === "dismissed" ||
    card?.state === "expired" ||
    (card?.state === "confirmed" &&
      outcomes.length > 0 &&
      outcomes.every((s) =>
        ["failed", "not_performed", "dry_run", "refused", "skipped"].includes(
          s,
        ),
      ));
  return (
    <li
      className="space-y-1.5 border-l-2 border-accent pl-3"
      data-testid="prepared-change"
    >
      <p className="text-[13px] leading-5 font-medium">
        {card?.title || "Prepared change"}
      </p>
      {!card && (
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => void run(() => getPrepared(id))}
        >
          Review change
        </Button>
      )}
      {card && (
        <div className="text-[13px] leading-5">
          {card.reason && <p className="text-muted">{card.reason}</p>}
          {(card.actions ?? []).map((a, i) => (
            <ActionShown key={i} action={a} />
          ))}
          <table
            className="mt-1 w-full text-[12px] leading-4"
            data-testid="prepared-diff"
          >
            <thead>
              <tr className="text-left text-muted">
                <th className="pr-3 font-normal">Field</th>
                <th className="pr-3 font-normal">
                  Before (as the system holds it)
                </th>
                <th className="font-normal">After</th>
              </tr>
            </thead>
            <tbody>
              {(card.diff ?? []).map((d, i) => (
                <tr key={i} className="align-top">
                  <td className="pr-3">{d.field}</td>
                  <td className="pr-3 text-muted line-through">
                    {shown(d.before)}
                  </td>
                  <td>{shown(d.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(card.missing ?? []).map((p, i) => (
            <p key={i} className="text-[12px] leading-4 text-muted">
              {p}
            </p>
          ))}
          {open &&
            (card.acknowledge ?? []).map((p, i) => (
              <p key={`ack-${i}`} className="text-[12px] leading-4">
                {p}
              </p>
            ))}
          {(card.undo ?? []).filter(Boolean).map((u, i) => (
            <p key={`undo-${i}`} className="text-[12px] leading-4 text-muted">
              {u}
            </p>
          ))}
          {card.state !== "prepared" && (
            <p className="text-[12px] leading-4" data-testid="prepared-state">
              {STATES[card.state] ??
                (card.outcome ?? [])
                  .map((o) => DONE[o.status ?? ""] ?? o.status)
                  .join(" ")}
            </p>
          )}
          <div className="mt-1.5 flex gap-2">
            {open &&
              (armed ? (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    void run(() =>
                      preparedStep(cur, "confirm", card.preview_hash ?? ""),
                    )
                  }
                >
                  {card.live ? "Confirm: do it now" : "Confirm"}
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setArmed(true)}
                >
                  Approve
                </Button>
              ))}
            {open && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => void run(() => preparedStep(cur, "dismiss"))}
              >
                Dismiss
              </Button>
            )}
            {retry && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  void run(() =>
                    preparedStep(cur, "retry").then((c) =>
                      getPrepared(c.prepared_id),
                    ),
                  )
                }
              >
                Prepare again
              </Button>
            )}
            {card.state === "confirmed" && written && (
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => void run(() => preparedStep(cur, "undo"))}
              >
                Undo
              </Button>
            )}
          </div>
        </div>
      )}
      {error && (
        <p className="text-[12px] leading-4 text-late" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

export function PreparedChanges({ ids }: { ids: string[] }) {
  if (ids.length === 0) return null;
  return (
    <ul className="space-y-3" aria-label="Prepared changes">
      {ids.map((id) => (
        <PreparedChange key={id} id={id} />
      ))}
    </ul>
  );
}
