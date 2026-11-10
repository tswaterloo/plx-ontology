"use client";

import Link from "next/link";
import { useState } from "react";
import { CardActions } from "@/components/card-actions";
import { prepareMyWorkWrite } from "@/lib/api";
import { plainError } from "@/lib/errors";
import { plural } from "@/lib/format";
import type { ActionResult, MyWork, MyWorkItem } from "@/lib/types";

type Link_ = { label?: string; href?: string };

// One prepared item: what happened, the prepared work, its evidence and one-click approve.
function Item({ item, state, onDone }: { item: MyWorkItem; state: ActionResult | null; onDone: (r: ActionResult) => void }) {
  const [entry, setEntry] = useState<string | null>(null);
  const prepared = (item.prepared ?? {}) as Record<string, unknown>;
  const draft = prepared.draft as { body?: string } | undefined;
  const links = (prepared.links as Link_[] | undefined) ?? [];
  const gaps = (prepared.gaps as string[] | undefined) ?? [];
  const happened = (item.happened ?? {}) as Record<string, unknown>;
  const objectId = typeof happened.object_id === "string" ? happened.object_id : null;
  return (
    <li className="space-y-1 rounded-lg border border-border p-3 text-[13px]" data-testid="my-work-item">
      <div className="font-medium">{item.title}</div>
      <div className="text-muted">
        {item.label} · due {item.due_at.slice(0, 16).replace("T", " ")} UTC{item.mine ? "" : " · a report's item (view only)"}
      </div>
      {objectId && (
        <Link className="font-medium hover:underline" href={`/object/${encodeURIComponent(objectId.split(":")[0])}/${encodeURIComponent(objectId)}`}>
          What happened
        </Link>
      )}
      {typeof prepared.summary === "string" && <p>{prepared.summary}</p>}
      {links.map((l) => l.href && (
        <Link key={l.href} className="mr-3 hover:underline" href={l.href}>{l.label ?? "Open"}</Link>
      ))}
      {gaps.length > 0 && <p className="text-muted">Missing: {gaps.join(", ").replaceAll("_", " ")}</p>}
      {draft?.body && <pre className="whitespace-pre-wrap rounded bg-hover p-2 text-[12px]">{draft.body}</pre>}
      <p className="text-[11px] text-muted">{plural((item.citations ?? []).length, "source")} cited</p>
      {item.mine && item.card_id && (
        <CardActions card={{ id: item.card_id, subject: { label: item.title }, actions: item.offers }} state={state} onDone={onDone} />
      )}
      {item.mine && item.write_ready && (
        <button
          className="rounded border border-border px-2 py-1"
          data-testid="my-work-entry"
          onClick={() => prepareMyWorkWrite(item.item_id).then(() => setEntry("Entry prepared: preview and approve it in the write panel (dry run)."), (e) => setEntry(plainError(e)))}
        >
          Prepare the entry
        </button>
      )}
      {entry && <p className="text-muted">{entry}</p>}
    </li>
  );
}

export function MyWorkList({ work }: { work: MyWork }) {
  const [states, setStates] = useState<Record<string, ActionResult>>({});
  const items = work.items ?? [];
  return (
    <div className="space-y-4" data-testid="my-work">
      {work.status === "off" && <p className="text-[13px] font-medium">The autopilot is switched off: nothing new is prepared and nothing can be approved.</p>}
      {items.length === 0 ? (
        <p className="text-[13px] text-muted">Nothing is waiting on you.</p>
      ) : (
        <p className="text-[13px] text-muted">
          {plural(work.digest.open, "item")} prepared, {work.digest.due_today} due today, {(work.digest.nudges ?? []).length} past due. Nothing is sent or written until you confirm it.
        </p>
      )}
      <ul className="space-y-3">
        {items.map((item) => (
          <Item key={item.item_id} item={item} state={states[item.card_id ?? ""] ?? null} onDone={(r) => setStates((s) => ({ ...s, [item.card_id as string]: r }))} />
        ))}
      </ul>
    </div>
  );
}
