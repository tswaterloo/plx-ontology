import { useCallback, useEffect, useState } from "react";
import type { HomeSectionId } from "./home-copy";

export type HomeLayout = "grouped" | "split";
export type HomeDensity = "comfortable" | "compact";

export interface HomePrefs {
  layout: HomeLayout;
  density: HomeDensity;
  visibleSections: Record<HomeSectionId, boolean>;
  collapsedSections: Record<HomeSectionId, boolean>;
}

export const HOME_PREFS_KEY = "workbench.home.prefs.v1";

export const SECTION_ORDER: HomeSectionId[] = ["replies", "holds_alerts", "tasks", "waiting"];

// guard-ok: display titles for typed Home workspace sections; decides nothing.
export const SECTION_LABELS: Record<HomeSectionId, string> = {
  replies: "Needs your reply",
  holds_alerts: "Alerts & late supply",
  tasks: "Tasks & commitments",
  waiting: "Waiting on others",
};

// guard-ok: plain-English subtitles for typed Home workspace sections; decides nothing.
export const SECTION_DESCRIPTIONS: Record<HomeSectionId, string> = {
  replies: "People waiting on your answer",
  holds_alerts: "Quality holds, alerts, and late purchase orders",
  tasks: "Your open commitments and action items",
  waiting: "Pending replies and follow-ups",
};

export const DEFAULT_HOME_PREFS: HomePrefs = {
  layout: "grouped",
  density: "comfortable",
  visibleSections: {
    replies: true,
    waiting: true,
    tasks: true,
    holds_alerts: true,
  },
  collapsedSections: {
    replies: false,
    waiting: false,
    tasks: false,
    holds_alerts: false,
  },
};

function sanitizePrefs(raw: unknown): HomePrefs {
  if (!raw || typeof raw !== "object") return DEFAULT_HOME_PREFS;
  const obj = raw as Record<string, unknown>;
  const layout: HomeLayout = obj.layout === "split" ? "split" : "grouped";
  const density: HomeDensity = obj.density === "compact" ? "compact" : "comfortable";
  const rawVis = obj.visibleSections && typeof obj.visibleSections === "object"
    ? (obj.visibleSections as Record<string, unknown>)
    : {};
  const rawCol = obj.collapsedSections && typeof obj.collapsedSections === "object"
    ? (obj.collapsedSections as Record<string, unknown>)
    : {};
  return {
    layout,
    density,
    visibleSections: {
      replies: typeof rawVis.replies === "boolean" ? rawVis.replies : true,
      waiting: typeof rawVis.waiting === "boolean" ? rawVis.waiting : true,
      tasks: typeof rawVis.tasks === "boolean" ? rawVis.tasks : true,
      holds_alerts: typeof rawVis.holds_alerts === "boolean" ? rawVis.holds_alerts : true,
    },
    collapsedSections: {
      replies: typeof rawCol.replies === "boolean" ? rawCol.replies : false,
      waiting: typeof rawCol.waiting === "boolean" ? rawCol.waiting : false,
      tasks: typeof rawCol.tasks === "boolean" ? rawCol.tasks : false,
      holds_alerts: typeof rawCol.holds_alerts === "boolean" ? rawCol.holds_alerts : false,
    },
  };
}

export function loadHomePrefs(): HomePrefs {
  if (typeof window === "undefined") return DEFAULT_HOME_PREFS;
  try {
    const saved = window.localStorage.getItem(HOME_PREFS_KEY);
    if (!saved) return DEFAULT_HOME_PREFS;
    return sanitizePrefs(JSON.parse(saved));
  } catch { // guard-ok: corrupted or unavailable localStorage falls back cleanly to default Home preferences
    return DEFAULT_HOME_PREFS;
  }
}

export function saveHomePrefs(prefs: HomePrefs): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(HOME_PREFS_KEY, JSON.stringify(prefs));
  } catch { // guard-ok: private-mode storage quota errors never break the workspace UI
    return;
  }
}

export function useHomePrefs() {
  const [prefs, setPrefs] = useState<HomePrefs>(DEFAULT_HOME_PREFS);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    setPrefs(loadHomePrefs());
    setHydrated(true);
  }, []);

  const update = useCallback((updater: (cur: HomePrefs) => HomePrefs) => {
    setPrefs((cur) => {
      const next = updater(cur);
      saveHomePrefs(next);
      return next;
    });
  }, []);

  const setLayout = useCallback(
    (layout: HomeLayout) => update((cur) => ({ ...cur, layout })),
    [update],
  );

  const setDensity = useCallback(
    (density: HomeDensity) => update((cur) => ({ ...cur, density })),
    [update],
  );

  const toggleSectionVisible = useCallback(
    (id: HomeSectionId) =>
      update((cur) => ({
        ...cur,
        visibleSections: { ...cur.visibleSections, [id]: !cur.visibleSections[id] },
      })),
    [update],
  );

  const toggleSectionCollapsed = useCallback(
    (id: HomeSectionId) =>
      update((cur) => ({
        ...cur,
        collapsedSections: { ...cur.collapsedSections, [id]: !cur.collapsedSections[id] },
      })),
    [update],
  );

  const resetPrefs = useCallback(() => {
    setPrefs(DEFAULT_HOME_PREFS);
    saveHomePrefs(DEFAULT_HOME_PREFS);
  }, []);

  const isCustomized =
    prefs.layout !== DEFAULT_HOME_PREFS.layout ||
    prefs.density !== DEFAULT_HOME_PREFS.density ||
    SECTION_ORDER.some((id) => !prefs.visibleSections[id] || prefs.collapsedSections[id]);

  return {
    prefs,
    hydrated,
    isCustomized,
    setLayout,
    setDensity,
    toggleSectionVisible,
    toggleSectionCollapsed,
    resetPrefs,
  };
}
