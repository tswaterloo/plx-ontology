/** The drafter's note (what the person fills in) and the check's warning, beside the draft. */
export function DraftNote({ note, warning }: { note?: string | null; warning?: string | null }) {
  if (!note && !warning) return null;
  return (
    <div className="space-y-1 px-1 text-[12px] leading-[18px]" data-testid="draft-note">
      {warning && <p className="text-warn">{warning}</p>}
      {note && <p className="text-muted">To fill in: {note}</p>}
    </div>
  );
}
