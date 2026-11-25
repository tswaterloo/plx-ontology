"use client";

import { createContext } from "react";

/** Set by a page that shows a person's pinned charts (Insights): called when an answer's chart
 * has been pinned there, so the page shows it at once (nothing else reads this). */
export const PinnedHere = createContext<(() => void) | null>(null);
