"use client";

// Fill a form for a record: pick the file (Word, Excel or PDF) and name the record
// ("PO 1234"). The server fills only the blanks one record value answers and lists every
// blank with its source; nothing is saved or sent from here.

import { Paperclip } from "lucide-react";
import { useRef, useState } from "react";
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
  const picker = useRef<HTMLInputElement>(null);
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
    <section className="space-y-2 border-t border-border pt-4" data-testid="form-fill">
      <div>
        <h2 className="text-[13px] leading-5 font-medium">Fill a form</h2>
        <p className="text-[12px] leading-4 text-muted">A Word, Excel or PDF form and the record it is for. You get a filled copy to check; nothing is saved or sent.</p>
      </div>
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <input
          ref={picker}
          id="form-fill-file"
          type="file"
          accept={FORMS}
          hidden
          data-testid="form-fill-file"
          onChange={(e) => {
            setFile(e.target.files?.[0] ?? null);
            setError(null);
          }}
        />
        <Button size="sm" variant="outline" type="button" onClick={() => picker.current?.click()} aria-label={file ? `Form: ${file.name}. Choose another` : "Choose the form"}>
          <Paperclip className="size-3.5" strokeWidth={2} aria-hidden />
          {file ? "Change form" : "Choose form"}
        </Button>
        {file && (
          <span className="max-w-[16rem] truncate text-[13px]" title={file.name} data-testid="form-fill-name">
            {file.name}
          </span>
        )}
        <label className="sr-only" htmlFor="form-fill-record">
          Record
        </label>
        <input
          id="form-fill-record"
          value={record}
          onChange={(e) => setRecord(e.target.value)}
          placeholder="Record, e.g. PO 1234"
          className="w-48 rounded-[3px] border border-border bg-bg px-2.5 py-1.5 text-[13px]"
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
