"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getMemoryPeople } from "@/lib/api";
import { useViewingAs } from "@/components/view-as";
import { eastern } from "@/lib/format";
import type { MemoryPeople } from "@/lib/types";

/** "Ana Lopez" from "ana.lopez@x.com": the address's own parts, as the server names people. */
export function nameOf(address: string): string {
  const parts = address.split("@")[0].split(/[._\-+]+/).filter((p) => p && !/^\d+$/.test(p));
  return parts.map((p) => p[0].toUpperCase() + p.slice(1)).join(" ") || address;
}

function askAbout(q: string): string {
  return `/ask?q=${encodeURIComponent(q)}`;
}

/**
 * The people the reader works with most and their conversations going (whose turn it is),
 * read from their own mailbox each time the page opens; nothing kept. Says so plainly when
 * the mail cannot be read, never a guess.
 */
export function MemoryPeopleSection() {
  const [data, setData] = useState<MemoryPeople | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const c = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let tries = 0;
    // a cold mailbox is read in the background: ask again while the server says reading
    const load = () =>
      getMemoryPeople(c.signal)
        .then((got) => {
          if (c.signal.aborted) return;
          if (got.reading && tries++ < 15) timer = setTimeout(load, 2000);
          else setData(got);
        })
        .catch(() => !c.signal.aborted && setFailed(true));
    load();
    return () => {
      c.abort();
      if (timer) clearTimeout(timer);
    };
  }, []);
  if (failed || (data && !data.available)) {
    return <p className="mb-6 text-[12px] text-muted">The people you work with can&rsquo;t be read from your mail right now.</p>;
  }
  if (!data) return null;
  const threads = data.threads ?? [];
  const people = data.people ?? [];
  const yours = threads.filter((t) => t.turn === "yours").length;
  return (
    <>
      {threads.length > 0 && (
        <section className="mb-6" aria-labelledby="memory-threads">
          <h2 id="memory-threads" className="text-[15px] font-semibold">
            Conversations going <span className="font-normal text-muted">{threads.length}</span>
          </h2>
          <p className="text-[12px] text-muted">
            Mail both ways in the last two weeks{yours ? `; ${yours} where the last message came to you` : ""}.
          </p>
          <ul className="divide-y divide-border">
            {threads.map((t) => (
              <li key={t.subject} className="flex items-baseline gap-3 py-2 text-[13px]" data-testid="memory-thread">
                <span className={t.turn === "yours" ? "w-24 shrink-0 font-medium" : "w-24 shrink-0 text-muted"}>
                  {t.turn === "yours" ? "Your turn" : "Waiting on them"}
                </span>
                <span className="min-w-0 flex-1">
                  <Link href={askAbout(`Where does "${t.subject}" stand?`)} className="hover:underline">{t.subject}</Link>
                  <span className="block truncate text-[12px] text-faint">
                    {t.others.map(nameOf).join(", ")} · {t.messages} messages · {eastern(t.last_at)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {people.length > 0 && (
        <section className="mb-6" aria-labelledby="memory-people">
          <h2 id="memory-people" className="text-[15px] font-semibold">
            People you work with <span className="font-normal text-muted">{people.length}</span>
          </h2>
          <p className="text-[12px] text-muted">The most mail with you in your recent mail, up to 90 days back. The Ask box suggests recipients from the same mail.</p>
          <ul className="divide-y divide-border">
            {people.map((p) => (
              <li key={p.address} className="flex items-baseline gap-3 py-2 text-[13px]" data-testid="memory-person">
                <span className="min-w-0 flex-1">
                  <Link href={askAbout(`Open items with ${p.address}`)} className="font-medium hover:underline">{p.name}</Link>
                  <span className="text-muted"> {p.address}{p.inside ? " · colleague" : ""}</span>
                  {p.last_subject && <span className="block truncate text-[12px] text-faint">Last: {p.last_subject}</span>}
                </span>
                <span className="shrink-0 text-[12px] tabular-nums text-muted">
                  {p.sent} sent · {p.received} in · {eastern(p.last_at)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/**
 * Today's one line of conversations where the last message came to the reader, opening
 * them in Memory. Nothing when there are none, the mail is not read yet, or viewing as
 * someone; Today never waits on it.
 */
export function YourTurn() {
  const viewing = useViewingAs();
  const [yours, setYours] = useState(0);
  useEffect(() => {
    if (viewing) return;
    const c = new AbortController();
    getMemoryPeople(c.signal)
      .then((got) => !c.signal.aborted && setYours(got.available ? (got.threads ?? []).filter((t) => t.turn === "yours").length : 0))
      .catch(() => undefined);
    return () => c.abort();
  }, [viewing]);
  if (viewing || yours === 0) return null;
  return (
    <div className="border-b border-border px-5 py-2 text-[13px]" data-testid="your-turn">
      <Link href="/memory#memory-threads" className="hover:underline">
        Your turn in {yours} {yours === 1 ? "conversation" : "conversations"}
      </Link>
    </div>
  );
}
