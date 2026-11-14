"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { getAutopilot, getOpsAlarms } from "@/lib/api";
import { capNotices, desktopBridge, freshOnes, menuTarget, type Pending } from "@/lib/desktop";
import { useAuth } from "./auth";
import { useFeedbackDialog } from "./feedback-dialog";

// A prepared reply reaches My work seconds after its message is captured (the autopilot's
// fast feed): the notice follows within a minute.
const EVERY_MS = 60_000;

/**
 * Inside the desktop app only: the menu's choices (Give feedback, Today, Ask...) and system
 * notifications for what newly needs the person: a new reliability alarm (ops readers) and a
 * new Autopilot item waiting for their decision. It reads what this page already reads, as
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
    const ctl = new AbortController();
    async function poll() {
      const now: Pending[] = [];
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
