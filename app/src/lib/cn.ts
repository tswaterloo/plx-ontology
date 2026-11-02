import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// The type scale's names (tokens.css) are font sizes, not colours: without this,
// cn("text-meta text-muted") would keep only one of them.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["display", "title", "answer", "section", "body", "row", "meta", "label", "stat", "eyebrow"] }],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
