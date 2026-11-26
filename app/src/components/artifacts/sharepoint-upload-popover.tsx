"use client";

import {
  ArrowUpRight,
  Check,
  CheckCircle2,
  Download,
  FolderPlus,
  FolderUp,
  Loader2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  uploadArtifactToSharePoint,
  type SharePointFolderInfo,
  type SharePointUploadResponse,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import type { ArtifactBadgeType, GalleryArtifact } from "./artifacts-studio";

const UPLOAD_FORMATS = [
  { id: "xlsx", label: ".xlsx" },
  { id: "docx", label: ".docx" },
  { id: "pptx", label: ".pptx" },
  { id: "csv", label: ".csv" },
  { id: "pdf", label: ".pdf" },
] as const;

function defaultFormatForType(type: ArtifactBadgeType): string {
  switch (type) {
    case "Sheet":
      return "xlsx";
    case "Slides":
      return "pptx";
    case "Chart":
      return "xlsx";
    case "Doc":
    case "Workflow":
    default:
      return "docx";
  }
}

export function SharePointUploadPopover({
  artifact,
  folders,
  onClose,
}: {
  artifact: GalleryArtifact;
  folders: SharePointFolderInfo[];
  onClose: () => void;
}) {
  const defaultFolder = useMemo(() => {
    if (artifact.domain) {
      const match = folders.find((f) =>
        f.path.toLowerCase().includes(artifact.domain!.toLowerCase()),
      );
      if (match) return match.path;
      return `Shared Documents/${artifact.domain}`;
    }
    return folders[0]?.path || "Shared Documents/Operations";
  }, [artifact.domain, folders]);

  const [folderPath, setFolderPath] = useState(defaultFolder);
  const [format, setFormat] = useState(() => defaultFormatForType(artifact.type));
  const [createIfMissing, setCreateIfMissing] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SharePointUploadResponse | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const visibleFolders = useMemo(() => {
    const q = folderPath.trim().toLowerCase();
    const sorted = folders.slice(0, 14);
    if (!q) return sorted.slice(0, 8);
    const matching = folders.filter((f) => f.path.toLowerCase().includes(q));
    return (matching.length > 0 ? matching : sorted).slice(0, 8);
  }, [folderPath, folders]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const target = folderPath.trim();
    if (!target) {
      setError("Enter a destination SharePoint folder path.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await uploadArtifactToSharePoint({
        title: artifact.title,
        format,
        folder_path: target,
        create_folder_if_missing: createIfMissing,
        artifact_id: artifact.id,
        source_url: artifact.href,
      });
      setResult(res);
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : "Could not prepare SharePoint upload. Try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="sp-upload-title"
        data-testid="sharepoint-upload-popover"
        className="w-full max-w-[480px] rounded-xl border border-border-strong bg-surface shadow-2xl"
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0 space-y-0.5">
            <div className="flex items-center gap-1.5 text-[11.5px] font-medium text-muted">
              <FolderUp className="size-3.5" aria-hidden />
              <span>Save to SharePoint</span>
            </div>
            <h3
              id="sp-upload-title"
              className="truncate text-[14.5px] font-semibold tracking-[-0.01em] text-fg"
            >
              {artifact.title}
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close SharePoint dialog"
            className="flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted transition-colors hover:bg-hover hover:text-fg"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>

        {result ? (
          <div className="space-y-4 p-5" data-testid="sp-upload-result">
            <div
              className={cn(
                "rounded-lg border p-3.5 text-[13px]",
                result.status === "uploaded"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-fg"
                  : "border-border-strong bg-surface-2/80 text-fg",
              )}
            >
              <div className="flex items-start gap-2.5">
                <CheckCircle2
                  className={cn(
                    "mt-0.5 size-4 shrink-0",
                    result.status === "uploaded" ? "text-emerald-400" : "text-fg",
                  )}
                  aria-hidden
                />
                <div className="min-w-0 space-y-1">
                  <p className="font-semibold">
                    {result.status === "uploaded"
                      ? "Uploaded to SharePoint"
                      : "Queued for SharePoint approval"}
                  </p>
                  <p className="text-[12.5px] leading-relaxed text-muted">{result.message}</p>
                </div>
              </div>

              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-border/70 pt-2.5 text-[12px]">
                <dt className="text-faint">File</dt>
                <dd className="truncate font-mono text-fg">{result.filename}</dd>
                <dt className="text-faint">Folder</dt>
                <dd className="truncate font-mono text-fg">{result.folder_path}</dd>
                {result.draft_id && (
                  <>
                    <dt className="text-faint">Draft ID</dt>
                    <dd className="truncate font-mono text-muted">{result.draft_id}</dd>
                  </>
                )}
              </dl>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                {result.download_url && (
                  <a
                    href={result.download_url}
                    download={result.filename}
                    data-testid="sp-upload-download"
                    className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border-strong bg-bg px-3 text-[12.5px] font-medium text-fg transition-colors hover:bg-hover"
                  >
                    <Download className="size-3.5" aria-hidden />
                    <span>Download file</span>
                  </a>
                )}
                {result.status === "uploaded" && result.web_url ? (
                  <a
                    href={result.web_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-colors hover:opacity-90"
                  >
                    <span>Open in SharePoint</span>
                    <ArrowUpRight className="size-3.5" aria-hidden />
                  </a>
                ) : (
                  <Link
                    href={result.assist_url || "/assist"}
                    data-testid="sp-upload-approve-assist"
                    className="inline-flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-[12.5px] font-medium text-accent-fg transition-colors hover:opacity-90"
                  >
                    <span>Approve in Assist</span>
                    <ArrowUpRight className="size-3.5" aria-hidden />
                  </Link>
                )}
              </div>
              <Button type="button" variant="secondary" size="sm" onClick={onClose}>
                Done
              </Button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4 p-5">
            <div className="space-y-1.5">
              <label htmlFor="sp-upload-folder-input" className="block text-[12px] font-medium text-muted">
                Destination SharePoint folder
              </label>
              <input
                id="sp-upload-folder-input"
                type="text"
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
                placeholder="Shared Documents/Operations"
                data-testid="sp-upload-folder-input"
                className="h-9 w-full rounded-md border border-border-strong bg-bg px-3 text-[13px] text-fg placeholder:text-faint focus:border-accent/60 focus:outline-none"
              />
              {visibleFolders.length > 0 && (
                <div className="max-h-36 space-y-1 overflow-y-auto rounded-md border border-border bg-bg/70 p-1.5">
                  {visibleFolders.map((f) => {
                    const selected = f.path.toLowerCase() === folderPath.trim().toLowerCase();
                    return (
                      <button
                        key={f.path}
                        type="button"
                        onClick={() => setFolderPath(f.path)}
                        className={cn(
                          "flex w-full cursor-pointer items-center justify-between gap-2 rounded px-2 py-1 text-left text-[12px] transition-colors",
                          selected
                            ? "bg-surface-2 font-medium text-fg"
                            : "text-muted hover:bg-hover hover:text-fg",
                        )}
                      >
                        <span className="truncate font-mono text-[11.5px]">{f.path}</span>
                        <span className="flex shrink-0 items-center gap-1.5 text-[11px] text-faint">
                          {f.file_count > 0 && <span className="num">{f.file_count} files</span>}
                          {selected && <Check className="size-3 text-fg" aria-hidden />}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="space-y-1.5">
              <span className="block text-[12px] font-medium text-muted">File format</span>
              <div
                role="group"
                aria-label="Export file format"
                className="liquid-glass-pill inline-flex flex-wrap items-center gap-0.5 rounded-lg p-0.5"
              >
                {UPLOAD_FORMATS.map((opt) => {
                  const active = format === opt.id;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setFormat(opt.id)}
                      className={cn(
                        "inline-flex h-7 cursor-pointer items-center rounded-md px-2.5 font-mono text-[11.5px] font-medium transition-colors",
                        active ? "liquid-glass-active" : "text-muted hover:text-fg",
                      )}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <label className="flex cursor-pointer items-center gap-2 text-[12.5px] text-muted select-none hover:text-fg">
              <input
                type="checkbox"
                checked={createIfMissing}
                onChange={(e) => setCreateIfMissing(e.target.checked)}
                data-testid="sp-upload-create-folder"
                className="size-3.5 cursor-pointer rounded border-border-strong accent-[var(--accent)]"
              />
              <FolderPlus className="size-3.5 text-muted" aria-hidden />
              <span>Create folder path if it does not exist yet</span>
            </label>

            {error && (
              <p
                role="alert"
                data-testid="sp-upload-error"
                className="rounded-md border border-late/40 bg-late/10 px-3 py-2 text-[12.5px] text-late"
              >
                {error}
              </p>
            )}

            <div className="flex items-center justify-end gap-2 border-t border-border pt-3.5">
              <Button type="button" variant="secondary" size="sm" onClick={onClose}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                size="md"
                disabled={submitting || !folderPath.trim()}
                data-testid="sp-upload-submit"
              >
                {submitting ? (
                  <>
                    <Loader2 className="size-3.5 animate-spin" aria-hidden />
                    <span>Preparing...</span>
                  </>
                ) : (
                  <>
                    <FolderUp className="size-3.5" aria-hidden />
                    <span>Upload to SharePoint</span>
                  </>
                )}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
