/**
 * Who the lists are for in the menus (the owner, 11-16: they should not clutter everyone's
 * menu; the MRP testing list is not ours and is gone). Only the menu entries are scoped: the
 * sidebar's Lists block, the phone More menu and the command bar. The page stays reachable
 * by its URL and shows only the items its reader may open (the server decides that).
 */

export interface ListsReader {
  owner?: boolean;
  roles?: string[] | null;
}

export interface ListPlace {
  href: string;
  label: string;
}

/** Work roles the R&D and sample projects list is for: R&D, and sales / customer service. */
const RND_ROLES = new Set(["rnd", "customer_service", "sales"]);

/** The owner, or a reader whose work role is R&D or sales / customer service. */
export function seesRndProjects(me: ListsReader | null | undefined): boolean {
  return !!me && (!!me.owner || (me.roles ?? []).some((r) => RND_ROLES.has(r)));
}

const GATES: Record<string, (me: ListsReader | null | undefined) => boolean> = {
  "/rd-projects": seesRndProjects,
};

/** Whether a list's menu entry is for this reader; a path that is not one of the lists is not gated. */
export function listShownTo(href: string, me: ListsReader | null | undefined): boolean {
  const gate = GATES[href];
  return gate ? gate(me) : true;
}

export const LISTS: ListPlace[] = [
  { href: "/rd-projects", label: "R&D and sample projects" },
];

/** The lists this reader's menus show (none: the Lists block is not drawn). */
export function listsFor(me: ListsReader | null | undefined): ListPlace[] {
  return LISTS.filter((p) => listShownTo(p.href, me));
}
