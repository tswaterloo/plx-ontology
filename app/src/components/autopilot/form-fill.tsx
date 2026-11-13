"use client";

// Fill a form for a record: pick the file (Word, Excel or PDF) and name the record
// ("PO 1234"). The server fills only the blanks one record value answers and lists every
// blank with its source; nothing is saved or sent from here.

import { useState } from "react";
import { fillForm } from "@/lib/api";
import { MAX_BYTES } from "@/lib/attachments";
import type { InstructFill } from "@/lib/types";
import { FillReview } from "../fill-review";
import { errorWords } from "../upload-draft";
import { Button } from "../ui/button";

const FORMS = ".docx,.xlsx,.pdf";

function base64Of(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export function FormFill() {
  const [file, setFile] = useState<File | null>(null);
  const [record, setRecord] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fill, setFill] = useState<InstructFill | null>(null);
  const submit = async () => {
    if (!file || !record.trim()) return;
    if (file.size > MAX_BYTES) {
      setError(`${file.name} is larger than ${MAX_BYTES / (1024 * 1024)} MB.`);
      return;
    }
    setBusy(true);
    setError(null);
    setFill(null);
    try {
      setFill(await fillForm({ name: file.name, data: await base64Of(file), record: record.trim() }));
    } catch (err) {
      setError(errorWords(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="mb-5 space-y-2" data-testid="form-fill">
      <h2 className="text-[13px] leading-5 font-medium">Fill a form</h2>
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="sr-only" htmlFor="form-fill-file">
          Form
        </label>
        <input id="form-fill-file" type="file" accept={FORMS} className="text-[13px]" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <label className="sr-only" htmlFor="form-fill-record">
          Record
        </label>
        <input
          id="form-fill-record"
          value={record}
          onChange={(e) => setRecord(e.target.value)}
          placeholder="PO 1234"
          className="w-40 rounded-[3px] border border-border bg-bg px-2.5 py-1.5 text-[13px]"
        />
        <Button size="sm" type="submit" disabled={busy || !file || !record.trim()}>
          {busy ? "Filling…" : "Fill"}
        </Button>
      </form>
      {error && (
        <p className="text-[12px] leading-4 text-late" role="alert">
          {error}
        </p>
      )}
      {fill && <FillReview fill={fill} />}
    </section>
  );
}
