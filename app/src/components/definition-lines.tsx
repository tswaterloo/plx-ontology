import { ShieldCheck } from "lucide-react";
import { formatDate } from "@/lib/format";
import type { MeasureDefinition } from "@/lib/types";

type Definition = MeasureDefinition;

/** "Owner Finance, definition v78103d58, set 23 Nov 2026." (no day when the catalogue states none for this version). */
export function definitionMeta(d: Definition): string {
  return `Owner ${d.owner}, definition ${d.version}${d.defined_on ? `, set ${formatDate(d.defined_on)}` : ""}.`;
}

/** The agreed measure definitions a figure rests on, each with its Verified measure badge, owner, version and the day it was set: shown beside the figure, never folded away. */
export function DefinitionLines({ definitions, testId }: { definitions: readonly Definition[]; testId: string }) {
  if (definitions.length === 0) return null;
  return (
    <dl className="space-y-1.5 text-[13px]" data-testid={testId}>
      {definitions.map((d) => (
        <div key={d.id} className="leading-5">
          <span
            className="mr-1.5 inline-flex items-center gap-1 rounded border border-ok/30 bg-ok/10 px-1.5 py-0.5 align-middle text-[11px] font-medium leading-none text-ok"
            data-testid="verified-measure-badge"
          >
            <ShieldCheck className="size-3 shrink-0" strokeWidth={2.2} aria-hidden />
            Verified measure
          </span>
          <dt className="inline font-medium">{d.name} (agreed definition): </dt>
          <dd className="inline">
            {d.definition} <span className="text-muted">{definitionMeta(d)}</span>
          </dd>
        </div>
      ))}
    </dl>
  );
}
