"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useAuth } from "@/components/auth";
import type { Me } from "@/lib/types";

/** Where a reader's "seen it" mark for the first-week note is kept (this browser only). */
export const firstWeekKey = (principal: string) => `plx.first-week.done.${principal}`;

/**
 * The first-week note on My work for a reader new to the Workbench: what the app shows
 * them, what it leaves out, and where to tell us when something is wrong. Owners reading
 * with full access never see it. Closing it keeps it closed in this browser.
 */
export function FirstWeek({ me }: { me: Me | null }) {
  const [open, setOpen] = useState(false);
  const key = me ? firstWeekKey(me.principal) : null;
  useEffect(() => {
    if (!me || !key || me.access_label || me.can_view_as) return setOpen(false);
    try {
      setOpen(window.localStorage.getItem(key) === null);
    } catch {
      setOpen(false);
    }
  }, [me, key]);
  if (!open || !key) return null;
  const close = () => {
    try {
      window.localStorage.setItem(key, new Date().toISOString());
    } catch {
      /* private mode: it shows again next time */
    }
    setOpen(false);
  };
  return (
    <section aria-label="Your first week" className="border-b border-border px-5 py-3" data-testid="first-week">
      <h2 className="text-[13px] font-semibold">Your first week</h2>
      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[12px] text-muted">
        <li>My work lists what needs you today from your own mail, chats and records.</li>
        <li>Search or Ask finds any order, supplier or customer by its number or name. Every answer links to the mail or document it came from.</li>
        <li>You see what your Portal access allows. Other people&apos;s mailboxes, payroll and HR files stay hidden unless your role covers them.</li>
        <li>Nothing is sent or changed for you. Autopilot stays off until you turn it on yourself, one workflow at a time.</li>
        <li>
          <Link href="/help" className="underline" data-testid="first-week-help">
            Read the guide for your role
          </Link>{" "}
          and tick off your first-week checklist.
        </li>
        <li>
          If something is wrong or missing, say so on <Link href="/feedback" className="underline">Feedback</Link>. We read every note.
        </li>
      </ul>
      <button type="button" onClick={close} className="mt-2 text-[12px] font-medium text-muted hover:text-fg cursor-pointer" data-testid="first-week-close">
        Got it
      </button>
    </section>
  );
}

/** The first-week note for the signed-in reader. */
export function SignedInFirstWeek() {
  const { state } = useAuth();
  return <FirstWeek me={state.status === "signed-in" ? state.me : null} />;
}
