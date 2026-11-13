"use client";

// A drafted file's upload to SharePoint (an instruction's document, a filled form's copy):
// preview before and after, then Approve and a second Confirm click. The upload runs
// through the gated write path: recorded as a dry run until the owner opens SharePoint.

import { useState } from "react";
import { approveUpload, getUpload } from "@/lib/api";
import { sizeWords } from "@/lib/attachments";
import { ApiError, plainError } from "@/lib/errors";
import type { InstructDocument, UploadPreview } from "@/lib/types";
import { Button } from "./ui/button";

export function errorWords(err: unknown): string {
  // the server's refusals are its fixed sentences
  return err instanceof ApiError && err.detail ? err.detail : plainError(err);
}

export function Upload({ doc }: { doc: InstructDocument }) {
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
