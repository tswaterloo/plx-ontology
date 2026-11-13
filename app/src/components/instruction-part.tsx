"use client";

// What the Ask box came back with when it was told to do something (answer.instruction):
// its one question (with the meetings it would use), the "draft it or answer it" choice,
// exact changes with their values now and after, documents with their SharePoint upload,
// and a proposed workflow in Draft mode. The drafted emails and actions show beside it
// (AskDrafts). Nothing is sent, written or uploaded from here: every draft has its own
// preview and Confirm click.

import { useState } from "react";
import type { AskRequest, InstructPart, InstructWrite } from "@/lib/types";
import { FillReview } from "./fill-review";
import { Upload } from "./upload-draft";
import { WritePanel } from "./write-panel";
import { Button } from "./ui/button";

const show = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : String(v));

/** The fields an Ask carries to continue an instruction on its own page. */
export type InstructAsk = Pick<AskRequest, "object_id" | "source" | "context" | "mode" | "instruction">;

/** The page an instruction was given on, as the Ask request states it again. */
export function pageOf(context: Record<string, unknown>): InstructAsk {
  if (context.kind === "email" && typeof context.handle === "string") return { source: context.handle };
  if (context.kind === "record" && typeof context.object_id === "string") return { object_id: context.object_id };
  return { context: context as AskRequest["context"] };
}

function Writes({ writes }: { writes: InstructWrite[] }) {
  if (!writes.length) return null;
  const ids = writes.map((w) => w.intent_id ?? "").filter(Boolean);
  return (
    <section data-testid="instruct-writes">
      <h3 className="mb-1.5 text-[12px] leading-4 font-medium text-muted">Changes to approve</h3>
      {writes.map((w, i) => (
        <div key={i} className="mb-2 border-l-2 border-accent pl-3">
          <p className="text-[13px] leading-5 font-medium">{w.label}</p>
          {(w.changes ?? []).some((c) => c.changed) && (
            <table className="mt-1 text-[13px] leading-5" data-testid="instruct-change">
              <thead className="text-left text-[12px] text-muted">
                <tr>
                  <th className="pr-4 font-normal">Field</th>
                  <th className="pr-4 font-normal">Now</th>
                  <th className="font-normal">After</th>
                </tr>
              </thead>
              <tbody>
                {(w.changes ?? [])
                  .filter((c) => c.changed)
                  .map((c) => (
                    <tr key={c.field}>
                      <td className="pr-4">{c.field}</td>
                      <td className="pr-4 text-muted line-through">{show(c.current)}</td>
                      <td>{show(c.proposed)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
          {(w.problems ?? []).map((p, j) => (
            <p key={j} className="text-[12px] leading-4 text-muted">
              {p}
            </p>
          ))}
          {w.status === "prepared" && <p className="text-[12px] leading-4 text-muted">A colleague who holds the write grant approves it on the write desk.</p>}
        </div>
      ))}
      {ids.length > 0 && <WritePanel match={ids} />}
    </section>
  );
}

export function InstructionPart({
  part,
  answerId,
  onInstruct,
}: {
  part: InstructPart;
  answerId: string;
  /** Ask again in the same conversation (absent while an answer is streaming). */
  onInstruct?: (question: string, extra: InstructAsk) => void;
}) {
  const [answer, setAnswer] = useState("");
  const busy = !onInstruct;
  const page = pageOf((part.context ?? {}) as Record<string, unknown>);
  const follow = (question: string, f: { answer?: string; use?: string[] }) =>
    onInstruct?.(question, { ...page, instruction: { instruction_id: answerId, answer: f.answer ?? null, use: f.use ?? [] } });
  const proposed = new Set((part.proposed ?? []).map((p) => p.meeting_id));
  const others = (part.meetings ?? []).filter((m) => !proposed.has(m.meeting_id));
  return (
    <div className="space-y-3" data-testid="instruction-part">
      {(part.read ?? []).length > 0 && (
        <p className="text-[12px] leading-4 text-muted" data-testid="instruct-read">
          Read for you:{" "}
          {(part.read ?? []).map((x, i) => (
            <span key={i}>
              {i > 0 && ", "}
              {x.open_url ? (
                <a className="text-accent hover:underline" href={x.open_url}>
                  {x.label}
                </a>
              ) : (
                x.label
              )}
            </span>
          ))}
        </p>
      )}
      {part.status === "route" && (
        <div className="flex gap-2" data-testid="instruct-route">
          <Button size="sm" disabled={busy} onClick={() => onInstruct?.(part.text, { ...page, mode: "do" })}>
            Draft it
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => onInstruct?.(part.text, { ...page, mode: "ask" })}>
            Answer it
          </Button>
        </div>
      )}
      {part.status === "question" && (
        <section className="space-y-2" data-testid="instruct-question">
          {(part.options ?? []).length > 0 && (
            <div className="flex flex-wrap gap-2" data-testid="instruct-options">
              {(part.options ?? []).map((o) => (
                <Button key={o} size="sm" variant="outline" disabled={busy} onClick={() => follow(o, { answer: o })}>
                  {o}
                </Button>
              ))}
            </div>
          )}
          {(part.proposed ?? []).map((m) => (
            <Button key={m.meeting_id} size="sm" disabled={busy} onClick={() => follow(`Use ${m.title}`, { use: [m.meeting_id] })}>
              Use {m.title} ({m.day})
            </Button>
          ))}
          {others.length > 0 && (
            <p className="text-[12px] leading-4 text-muted">
              Or use another meeting you attended:{" "}
              {others.map((m, i) => (
                <span key={m.meeting_id}>
                  {i > 0 && ", "}
                  <button type="button" className="text-accent hover:underline" disabled={busy} onClick={() => follow(`Use ${m.title}`, { use: [m.meeting_id] })}>
                    {m.title} ({m.day})
                  </button>
                </span>
              ))}
            </p>
          )}
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (answer.trim()) follow(answer.trim(), { answer: answer.trim() });
            }}
          >
            <label className="sr-only" htmlFor={`instruct-answer-${answerId}`}>
              Your answer
            </label>
            <input
              id={`instruct-answer-${answerId}`}
              className="min-w-0 flex-1 rounded-[3px] border border-border bg-bg px-2.5 py-1.5 text-[13px]"
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              placeholder="Answer in your own words"
            />
            <Button size="sm" type="submit" disabled={busy || !answer.trim()}>
              Answer
            </Button>
          </form>
        </section>
      )}
      {part.workflow && (
        <section className="border-l-2 border-accent pl-3 text-[13px] leading-5" data-testid="instruct-workflow">
          <p className="font-medium">{part.workflow.label}</p>
          <p>
            <span className="text-muted">When: </span>
            {part.workflow.trigger.words}
          </p>
          {part.workflow.actions.map((a, i) => (
            <p key={i}>
              <span className="text-muted">Prepares: </span>
              {a.words}
            </p>
          ))}
          <p className="text-[12px] leading-4 text-muted">Proposed in Draft mode: it prepares work for approval and never acts on its own. The Autopilot studio cannot keep workflows on this server yet; this proposal stays in your work log.</p>
        </section>
      )}
      <Writes writes={part.writes ?? []} />
      {(part.fills ?? []).map((f) => (
        <FillReview key={f.digest_before + f.record.object_id} fill={f} />
      ))}
      {(part.documents ?? []).length > 0 && (
        <section>
          <h3 className="mb-1.5 text-[12px] leading-4 font-medium text-muted">Documents</h3>
          <ul className="space-y-2">
            {(part.documents ?? []).map((d) => (
              <Upload key={d.upload_id} doc={d} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
