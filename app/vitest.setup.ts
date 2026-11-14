import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";

// findBy*/waitFor resolve as soon as the screen is right; the 1 s default only bounds how long a
// slow first render may take, and a loaded landing host misses it (a record page still
// "Loading the record" at 1 s with 40 busy processes on 4 cores). A wrong screen still fails.
configure({ asyncUtilTimeout: 10_000 });

afterEach(() => cleanup());

// jsdom has no matchMedia; the shell's phone buttons (in every page's Toolbar) ask it for the width
if (typeof window !== "undefined" && !window.matchMedia)
  window.matchMedia = (query: string) =>
    ({ matches: false, media: query, onchange: null, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }) as unknown as MediaQueryList;
