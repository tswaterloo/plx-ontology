import { Fragment, type ReactNode } from "react";
import type { Block, Inline } from "@/lib/markdown";

export function Inlines({ xs, map = (s) => s }: { xs: Inline[]; map?: (s: string) => string }) {
  return (
    <>
      {xs.map((x, i) => (x.bold ? <strong key={i} className="font-semibold">{checked(map(x.text))}</strong> : <Fragment key={i}>{checked(map(x.text))}</Fragment>))}
    </>
  );
}

/** A figure the answer check could not tie to a record it read, as the server marks it ("9 (unverified)"). */
const UNVERIFIED = /([^\s(]+) \(unverified\)/g;

/**
 * Text with each "<figure> (unverified)" shown as the figure under a dotted underline whose
 * tooltip says why: the check stays visible without breaking the sentence. The words stay for
 * screen readers (and copy), so nothing the server marked is dropped.
 */
export function checked(text: string): ReactNode {
  if (!text.includes("(unverified)")) return text;
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of text.matchAll(UNVERIFIED)) {
    out.push(text.slice(at, m.index));
    out.push(
      <span key={m.index} className="cursor-help underline decoration-late-line decoration-dotted decoration-[1.5px] underline-offset-[3px]" title="Not found in the records this answer read: check it before you rely on it" data-unverified="">
        {m[1]}
        <span className="sr-only"> (unverified)</span>
      </span>,
    );
    at = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(at));
  return out;
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
