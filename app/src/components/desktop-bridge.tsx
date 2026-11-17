"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { getAutopilot, getBriefing, getMemory, getOpsAlarms } from "@/lib/api";
import { morningNotice, MORNING_NOTICE_HOUR } from "@/lib/home";
import { viewingAs } from "@/lib/view-as";
import { capNotices, desktopBridge, freshOnes, menuTarget, type Pending } from "@/lib/desktop";
import { useAuth } from "./auth";
import { useFeedbackDialog } from "./feedback-dialog";
import { dueReminders } from "./your-commitments";

// A prepared reply reaches My work seconds after its message is captured (the autopilot's
// fast feed): the notice follows within a minute.
const EVERY_MS = 60_000;
// The person's own commitments are read every this many polls (a due day changes slowly).
const MEMORY_EVERY = 10;

/**
 * Inside the desktop app only: the menu's choices (Give feedback, Today, Ask...) and system
 * notifications for what newly needs the person: a new reliability alarm (ops readers) and a
 * new Autopilot item waiting for their decision, and once a day each of their own commitments
 * that is due or overdue (``dueReminders``, even when it was there when the app opened), and
 * once a morning from 8 AM what of their own the briefing holds (``morningNotice``). It reads what this page already reads, as
 * the signed-in reader; in a browser it renders nothing and runs nothing.
 */
export function DesktopBridge() {
  const router = useRouter();
  const feedback = useFeedbackDialog();
  const { state } = useAuth();
  const who = state.status === "signed-in" ? state.me.principal : null;
  const ops = state.status === "signed-in" && state.me.ops_reader === true;

  useEffect(() => {
    const bridge = desktopBridge();
    if (!bridge) return;
    return bridge.onMenu((msg) => {
      const t = menuTarget(msg);
      if (t?.kind === "feedback") feedback.open();
      else if (t?.kind === "go") router.push(t.href);
    });
  }, [router, feedback]);

  useEffect(() => {
    const bridge = desktopBridge();
    if (!bridge || !who) return;
    let live = true;
    let seen: Set<string> | null = null;
    let tick = 0;
    const ctl = new AbortController();
    async function poll() {
      const now: Pending[] = [];
      if (tick++ % MEMORY_EVERY === 0) {
        try {
          const memory = await getMemory(ctl.signal);
          if (live && memory.available) for (const n of capNotices(dueReminders(memory.items, window.localStorage), 3, "/")) bridge!.notify(n);
        } catch {
          /* memory unreadable: no reminder, and Today says nothing either */
        }
        // viewing as someone: their morning is not told to the one viewing
        if (!viewingAs() && new Date().getHours() >= MORNING_NOTICE_HOUR) {
          try {
            if (window.localStorage.getItem("plx.morning") !== new Date().toLocaleDateString("en-CA")) {
              const briefing = await getBriefing(ctl.signal);
              const told = live && !viewingAs() ? morningNotice(briefing, window.localStorage) : null;
              if (told) bridge!.notify({ title: told.title, body: told.body, href: told.href });
            }
          } catch {
            /* briefing or storage unreadable: Today says why when opened */
          }
        }
      }
      try {
        const area = await getAutopilot(ctl.signal);
        for (const i of area.items ?? []) {
          if (i.mine && i.state === "waiting") now.push({ id: `item:${i.item_id}`, title: "Waiting for your decision", body: i.title, href: `/autopilot?item=${encodeURIComponent(i.item_id)}` });
        }
      } catch {
        /* a failed read tells nobody anything */
      }
      if (ops) {
        try {
          const alarms = await getOpsAlarms(ctl.signal);
          for (const a of alarms.open ?? []) now.push({ id: `alarm:${a.key}`, title: "Reliability problem", body: a.line, href: "/ops" });
        } catch {
          /* the banner already says monitoring is unavailable */
        }
      }
      if (!live) return;
      for (const n of capNotices(freshOnes(seen, now))) bridge!.notify(n);
      seen = new Set([...(seen ?? []), ...now.map((p) => p.id)]);
    }
    void poll();
    const id = setInterval(poll, EVERY_MS);
    return () => {
      live = false;
      ctl.abort();
      clearInterval(id);
    };
  }, [who, ops]);

  return null;
}
