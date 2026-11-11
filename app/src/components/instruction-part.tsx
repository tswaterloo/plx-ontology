"use client";

// What the Ask box came back with when it was told to do something (answer.instruction):
// its one question (with the meetings it would use), the "draft it or answer it" choice,
// exact changes with their values now and after, documents with their SharePoint upload,
// and a proposed workflow in Draft mode. The drafted emails and actions show beside it
// (AskDrafts). Nothing is sent, written or uploaded from here: every draft has its own
// preview and Confirm click.

import { useState } from "react";
import { approveUpload, getUpload } from "@/lib/api";
import { sizeWords } from "@/lib/attachments";
import { ApiError, plainError } from "@/lib/errors";
import type { AskRequest, InstructDocument, InstructPart, InstructWrite, UploadPreview } from "@/lib/types";
import { WritePanel } from "./write-panel";
import { Button } from "./ui/button";

const show = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : String(v));

function errorWords(err: unknown): string {
  // the server's refusals are its fixed sentences
  return err instanceof ApiError && err.detail ? err.detail : plainError(err);
}

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

function Upload({ doc }: { doc: InstructDocument }) {
  const [shown, setShown] = useState<UploadPreview | null>(null);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (step: () => Promise<UploadPreview>) => {
    setBusy(true);
    setError(null);
    try {
      setShown(await step());
    } catch (err) {
      setError(errorWords(err));
    } finally {
      setBusy(false);
      setArmed(false);
    }
  };
  return (
    <li className="space-y-1.5 border-l-2 border-accent pl-3" data-testid="instruct-document">
      <p className="text-[13px] leading-5">
        <a className="font-medium text-accent hover:underline" href={doc.download_url} download={doc.name}>
          {doc.name}
        </a>{" "}
        <span className="text-muted">
          {doc.kind.toUpperCase()}, {sizeWords(doc.size)}
          {doc.folder ? `, to file in ${doc.folder}` : ", no SharePoint folder chosen"}
        </span>
      </p>
      {!shown && (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => getUpload(doc.upload_id))}>
          Preview upload
        </Button>
      )}
      {shown && (
        <div className="text-[13px] leading-5" data-testid="upload-preview">
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4">
            <dt className="text-muted">Before</dt>
            <dd>{shown.before}</dd>
            <dt className="text-muted">After</dt>
            <dd>{shown.after}</dd>
          </dl>
          {[...shown.problems, ...shown.missing].map((p, i) => (
            <p key={i} className="text-[12px] leading-4 text-muted">
              {p}
            </p>
          ))}
          {shown.status === "prepared" &&
            shown.problems.length === 0 &&
            (shown.acknowledge ?? []).map((p, i) => (
              <p key={`ack-${i}`} className="text-[12px] leading-4" data-testid="upload-acknowledge">
                {p}
              </p>
            ))}
          {shown.status !== "prepared" ? (
            <p className="text-[12px] leading-4">{UPLOAD_DONE[shown.status]}</p>
          ) : (
            shown.problems.length === 0 &&
            (armed ? (
              <Button size="sm" disabled={busy} onClick={() => void run(() => approveUpload(doc.upload_id, shown.preview_hash))}>
                {shown.missing.length === 0 ? "Confirm: upload it now" : "Confirm upload"}
              </Button>
            ) : (
              <Button size="sm" variant="outline" onClick={() => setArmed(true)}>
                Approve upload
              </Button>
            ))
          )}
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

// what an approved upload became (the server's state)
const UPLOAD_DONE: Record<string, string> = {
  would_upload: "Approved. Recorded as a dry run.",
  uploaded: "Uploaded to SharePoint.",
  failed: "Approved, but SharePoint refused the upload.",
  unknown: "Approved, but SharePoint did not answer. Check the folder before trying again.",
};

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
