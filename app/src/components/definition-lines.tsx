import { formatDate } from "@/lib/format";
import type { MeasureDefinition } from "@/lib/types";

type Definition = MeasureDefinition;

/** "Owner Finance, definition v78103d58, set 23 Nov 2026." (no day when the catalogue states none for this version). */
export function definitionMeta(d: Definition): string {
  return `Owner ${d.owner}, definition ${d.version}${d.defined_on ? `, set ${formatDate(d.defined_on)}` : ""}.`;
}

/** The agreed measure definitions a figure rests on, each with its owner, version and the day it was set: shown beside the figure, never folded away. */
export function DefinitionLines({ definitions, testId }: { definitions: readonly Definition[]; testId: string }) {
  if (definitions.length === 0) return null;
  return (
    <dl className="space-y-1 text-[13px]" data-testid={testId}>
      {definitions.map((d) => (
        <div key={d.id}>
          <dt className="inline font-medium">{d.name} (agreed definition): </dt>
          <dd className="inline">
            {d.definition} <span className="text-muted">{definitionMeta(d)}</span>
          </dd>
        </div>
      ))}
    </dl>
  );
}
