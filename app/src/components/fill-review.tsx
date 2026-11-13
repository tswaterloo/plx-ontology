"use client";

// A form filled from the records (answer.instruction.fills, or the Autopilot page's form
// filler): every blank with what it held, what was put there and the record field it came
// from, so each value is checked before anything is saved. The filled copy is a new file;
// the original is never changed. Saving it beside its SharePoint original is an upload the
// person approves; a reply carrying it is prepared, never sent from here.

import type { FillField, InstructFill } from "@/lib/types";
import { Upload } from "./upload-draft";

// why a blank was left as it was, in words
const LEFT: Record<string, string> = {
  kept: "Already filled in",
  person_only: "For whoever signs",
  unknown_field: "No rule for this label",
  no_fact: "Not in the records read",
  ambiguous: "The records disagree",
};

function Source({ f }: { f: FillField }) {
  if (f.status !== "filled" || !f.citation) return <span className="text-muted">{LEFT[f.status] ?? f.reason}</span>;
  const where = `${f.citation.record} · ${f.citation.field}`;
  return f.citation.open_url ? (
    <a className="text-accent hover:underline" href={f.citation.open_url}>
      {where}
    </a>
  ) : (
    <span>{where}</span>
  );
}

export function FillReview({ fill }: { fill: InstructFill }) {
  const filled = fill.counts.filled ?? 0;
  return (
    <section className="space-y-2 border-l-2 border-accent pl-3" data-testid="fill-review">
      <p className="text-[13px] leading-5">
        <span className="font-medium">{fill.original}</span> for{" "}
        <a className="text-accent hover:underline" href={fill.record.open_url ?? undefined}>
          {fill.record.label}
        </a>
        <span className="text-muted">
          {" "}
          · {filled} of {fill.fields.length} filled
        </span>
      </p>
      <div className="overflow-x-auto">
      <table className="w-full min-w-[32rem] text-[13px] leading-5" data-testid="fill-fields">
        <thead className="text-left text-[12px] text-muted">
          <tr>
            <th className="pr-4 font-normal">Field</th>
            <th className="pr-4 font-normal">Was</th>
            <th className="pr-4 font-normal">Now</th>
            <th className="font-normal">From</th>
          </tr>
        </thead>
        <tbody>
          {fill.fields.map((f, i) => (
            <tr key={i} data-status={f.status} className="align-top">
              <td className="pr-4">{f.label}</td>
              <td className="pr-4 text-muted">{f.before || "—"}</td>
              <td className={f.status === "filled" ? "pr-4 font-medium" : "pr-4 text-muted"}>{f.after || "—"}</td>
              <td className="text-[12px]">
                <Source f={f} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      {(fill.notes ?? []).map((n, i) => (
        <p key={i} className="text-[12px] leading-4 text-muted">
          {n}
        </p>
      ))}
      {fill.download_url && fill.upload_id ? (
        <ul>
          <Upload
            doc={{
              name: fill.name,
              kind: fill.kind,
              size: fill.size,
              digest: fill.digest_after ?? "",
              download_url: fill.download_url,
              upload_id: fill.upload_id,
              folder: fill.folder ?? null,
            }}
          />
        </ul>
      ) : fill.download_url ? (
        <p className="text-[13px] leading-5">
          <a className="font-medium text-accent hover:underline" href={fill.download_url} download={fill.name}>
            {fill.name}
          </a>{" "}
          <span className="text-muted">to download. Open the form from its SharePoint folder to save the copy beside it.</span>
        </p>
      ) : (
        <p className="text-[12px] leading-4 text-muted">Nothing could be filled from the records, so no copy was made.</p>
      )}
      {fill.reply && (
        <p className="text-[12px] leading-4 text-muted" data-testid="fill-reply">
          Reply to {fill.reply.to.join(", ") || "the sender"} with {fill.reply.attachment}: {fill.reply.missing.join(" ")}
        </p>
      )}
    </section>
  );
}
