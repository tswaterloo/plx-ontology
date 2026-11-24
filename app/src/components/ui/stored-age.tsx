import { StatusLine } from "@/components/ui/status-line";

/** A page that drew an earlier answer (this tab's, or the desktop app's store) while it reads the
 *  current one: says so, with when it was read, until the read returns. Renders nothing when `at`
 *  is null (the answer is current). */
export function StoredAge({ at, className }: { at: number | null; className?: string }) {
  if (at === null) return null;
  return <StatusLine className={className} value={{ status: "stale", as_of: new Date(at).toISOString(), why: "reading it again" }} />;
}
