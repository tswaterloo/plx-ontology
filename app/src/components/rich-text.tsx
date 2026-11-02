import { Fragment } from "react";
import type { Block, Inline } from "@/lib/markdown";

export function Inlines({ xs, map = (s) => s }: { xs: Inline[]; map?: (s: string) => string }) {
  return (
    <>
      {xs.map((x, i) => (x.bold ? <strong key={i} className="font-semibold">{map(x.text)}</strong> : <Fragment key={i}>{map(x.text)}</Fragment>))}
    </>
  );
}

/** Parsed markdown as React elements: paragraphs and bullet lists, bold kept, no HTML. */
export function RichText({ blocks, map, className }: { blocks: Block[]; map?: (s: string) => string; className?: string }) {
  return (
    <div className={className}>
      {blocks.map((b, i) =>
        b.kind === "p" ? (
          <p key={i} className={i > 0 ? "mt-3" : undefined}>
            <Inlines xs={b.inlines} map={map} />
          </p>
        ) : (
          <ul key={i} className={`${i > 0 ? "mt-3 " : ""}list-disc space-y-1 pl-5`}>
            {b.items.map((it, j) => (
              <li key={j}>
                <Inlines xs={it} map={map} />
              </li>
            ))}
          </ul>
        ),
      )}
    </div>
  );
}
