// A re-read of something the page has already drawn from an earlier answer (a stored page
// shown "as of 4 min ago") must not keep the page from being ready: the person already sees it
// complete, and the new answer swaps in behind. `quietly` marks the requests started inside it
// (api.ts `call` does not count them as the page's own reads).
let depth = 0;

/** Run `start` (it begins the re-read synchronously); its requests are background re-reads. */
export function quietly<T>(start: () => T): T {
  depth += 1;
  try {
    return start();
  } finally {
    depth -= 1;
  }
}

export const quiet = (): boolean => depth > 0;
