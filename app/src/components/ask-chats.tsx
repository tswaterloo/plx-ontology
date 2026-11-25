"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Ellipsis, PanelLeftClose, Pin, Search, SquarePen } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { getAskChats, updateAskChat } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { ago, formatDateTime } from "@/lib/format";
import { useAskRunning } from "@/lib/thread";
import type { AskChat, AskChats } from "@/lib/types";

/** Rows drawn at first; more are drawn as the list is scrolled, so every chat is reachable and none is cut. */
const DRAW_STEP = 60;

type Entry = { head: AskChat; same: AskChat[] };
const said = (c: AskChat) => c.title.trim().replace(/\s+/g, " ").toLowerCase();

/** One entry per distinct title, newest first; the older chats with the same title ride behind it, still reachable. A chat you named is its own entry. */
function collapse(chats: AskChat[]): Entry[] {
  const out: Entry[] = [];
  const seen = new Map<string, Entry>();
  for (const c of chats) {
    const key = c.named ? `n:${c.conversation_id}` : said(c);
    const hit = seen.get(key);
    if (hit) hit.same.push(c);
    else {
      const e = { head: c, same: [] };
      seen.set(key, e);
      out.push(e);
    }
  }
  return out;
}

const GROUPS = ["Today", "Yesterday", "Previous 7 days", "Older"] as const;
/** Which time heading a chat sits under, by the calendar days between now and its last question. */
function groupOf(last: string, now: Date): (typeof GROUPS)[number] {
  const t = new Date(last);
  const day = (d: Date) => Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((day(now) - day(t)) / 86_400_000);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days <= 7 ? "Previous 7 days" : "Older";
}

/**
 * Your Ask conversations, like a chat app's list: New chat and search on top, pinned first, then
 * your chats under Today, Yesterday, Previous 7 days and Older, one line each. The same question
 * asked again shows as one row with a count; the count opens the older ones. Open a chat to read
 * it and go on asking in it; rename, pin or delete it from its menu; search names and questions.
 * Deleting takes it off this list only: your work log keeps the record of what was asked.
 */
export function AskChatList({
  current,
  refreshKey,
  onNew,
  onCollapse,
  className,
}: {
  current?: string | null;
  refreshKey?: unknown;
  onNew: () => void;
  onCollapse?: () => void;
  className?: string;
}) {
  const [q, setQ] = useState("");
  const [list, setList] = useState<AskChats | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [drawn, setDrawn] = useState(DRAW_STEP);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const more = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const c = new AbortController();
    // a search waits for the typing to pause; the first read and a refresh go at once
    const t = setTimeout(
      () =>
        getAskChats(q, c.signal).then(
          (got) => {
            setList(got);
            setProblem(null);
          },
          (e) => {
            if (!c.signal.aborted) setProblem(plainError(e));
          },
        ),
      q ? 250 : 0,
    );
    return () => {
      clearTimeout(t);
      c.abort();
    };
  }, [q, refreshKey]);

  const change = useCallback(
    (chat: AskChat, body: { name?: string; pinned?: boolean; deleted?: boolean }) => {
      updateAskChat(chat.conversation_id, body).then(
        (got) => {
          setProblem(null);
          // the server answers with the whole list; a search in progress reads its own again
          if (q) getAskChats(q).then(setList, () => undefined);
          else setList(got);
          if (body.deleted && chat.conversation_id === current) onNew();
        },
        (e) => setProblem(plainError(e)),
      );
    },
    [q, current, onNew],
  );

  const { runningIds, runningChats } = useAskRunning();
  const baseChats = list?.chats ?? [];
  const mergedChats: AskChat[] = (() => {
    const out = baseChats.map((c) => (runningIds.has(c.conversation_id) ? { ...c, running: true } : c));
    if (q.trim()) return out;
    const seenIds = new Set(out.map((c) => c.conversation_id));
    const extras: AskChat[] = [];
    for (const rc of runningChats) {
      if (seenIds.has(rc.conversation_id)) continue;
      extras.push({
        conversation_id: rc.conversation_id,
        title: rc.title,
        named: false,
        pinned: false,
        questions: 1,
        last_at: rc.started_at,
        running: true,
      });
    }
    return extras.length ? [...extras, ...out] : out;
  })();
  const chats = mergedChats;
  const pinned = chats.filter((c) => c.pinned);
  const entries = collapse(chats.filter((c) => !c.pinned));
  // the rows still to draw: as the foot of the list scrolls into view, the next batch is drawn
  const total = entries.reduce((n, e) => n + 1 + (open.has(e.head.conversation_id) ? e.same.length : 0), 0);
  useEffect(() => {
    const el = more.current;
    if (!el || drawn >= entries.length) return;
    if (typeof IntersectionObserver === "undefined") {
      setDrawn(entries.length);
      return;
    }
    const io = new IntersectionObserver((hit) => hit.some((h) => h.isIntersecting) && setDrawn((n) => n + DRAW_STEP), { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
  }, [drawn, entries.length, total]);

  const now = new Date();
  const row = (chat: AskChat, extra?: { count?: number; open?: boolean; onToggle?: () => void; nested?: boolean }) => (
    <ChatRow
      key={chat.conversation_id}
      chat={chat}
      active={chat.conversation_id === current}
      renaming={renaming === chat.conversation_id}
      deleting={deleting === chat.conversation_id}
      count={extra?.count}
      expanded={extra?.open}
      onToggle={extra?.onToggle}
      nested={extra?.nested}
      onRename={(name) => {
        setRenaming(null);
        if (name !== null && name.trim() && name.trim() !== chat.title) change(chat, { name: name.trim() });
      }}
      onMenu={(what) => {
        if (what === "rename") setRenaming(chat.conversation_id);
        else if (what === "pin") change(chat, { pinned: !chat.pinned });
        else setDeleting(chat.conversation_id);
      }}
      onDelete={(sure) => {
        setDeleting(null);
        if (sure) change(chat, { deleted: true });
      }}
    />
  );
  const entry = (e: Entry) => {
    const id = e.head.conversation_id;
    const isOpen = open.has(id);
    return [
      row(e.head, {
        count: e.same.length ? e.same.length + 1 : undefined,
        open: isOpen,
        onToggle: () =>
          setOpen((cur) => {
            const next = new Set(cur);
            if (!next.delete(id)) next.add(id);
            return next;
          }),
      }),
      ...(isOpen ? e.same.map((c) => row(c, { nested: true })) : []),
    ];
  };

  const shown = entries.slice(0, drawn);
  const heading = "px-2 pt-3.5 pb-1 text-[10.5px] font-medium uppercase tracking-[0.05em] text-faint";
  return (
    <nav className={cn("flex min-h-0 flex-col bg-bg-subtle", className)} aria-label="Chats" data-testid="ask-chats">
      <div className="flex h-10 shrink-0 items-center justify-between gap-2 border-b border-border px-3">
        <span className="text-[12px] font-medium text-muted">Chats</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onNew}
            aria-label="New chat"
            title="New chat"
            className="inline-flex h-7 items-center gap-1.5 rounded-sm px-2 text-[12px] font-medium text-muted hover:bg-hover hover:text-fg cursor-pointer"
            data-testid="new-chat"
          >
            <SquarePen className="size-3.5 shrink-0" strokeWidth={2} aria-hidden />
            <span>New</span>
          </button>
          {onCollapse && (
            <button
              type="button"
              onClick={onCollapse}
              aria-label="Hide chats"
              title="Hide chats"
              data-testid="collapse-ask-chats"
              className="inline-flex size-7 items-center justify-center rounded-sm text-faint hover:bg-hover hover:text-fg cursor-pointer"
            >
              <PanelLeftClose className="size-3.5" strokeWidth={2} aria-hidden />
            </button>
          )}
        </div>
      </div>
      <label className="flex h-8 shrink-0 items-center gap-2 border-b border-border px-3 text-[12.5px] text-fg">
        <Search className="size-3.5 shrink-0 text-faint" strokeWidth={2} aria-hidden />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value.slice(0, 200))}
          placeholder="Search chats"
          aria-label="Search chats"
          className="min-w-0 flex-1 bg-transparent text-[12.5px] outline-none focus-visible:outline-none placeholder:text-faint"
        />
      </label>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-4">
        {problem && <p className="px-2.5 py-2 text-[12px] text-late" role="alert">{problem}</p>}
        {list && !list.available && <p className="px-2.5 py-2 text-[12px] text-muted">Your chats can't be listed just now.</p>}
        {list && list.available && chats.length === 0 && <p className="px-2.5 py-2 text-[12px] text-muted">{q ? "No chat matches that." : "Your questions will be listed here."}</p>}
        {pinned.length > 0 && (
          <section aria-label="Pinned">
            <h3 className={heading}>Pinned</h3>
            <ul className="space-y-0.5">{pinned.map((c) => row(c))}</ul>
          </section>
        )}
        {GROUPS.map((g) => {
          const mine = shown.filter((e) => groupOf(e.head.last_at, now) === g);
          if (mine.length === 0) return null;
          return (
            <section key={g} aria-label={g}>
              <h3 className={heading}>{g}</h3>
              <ul className="space-y-0.5">{mine.flatMap(entry)}</ul>
            </section>
          );
        })}
        {entries.length > drawn && <div ref={more} className="h-4" data-testid="more-chats" aria-hidden />}
      </div>
    </nav>
  );
}

function ChatRow({
  chat,
  active,
  renaming,
  deleting,
  onRename,
  onMenu,
  onDelete,
  count,
  expanded,
  onToggle,
  nested,
}: {
  count?: number;
  expanded?: boolean;
  onToggle?: () => void;
  /** An older chat shown under a collapsed row: it carries its own time, since the heading is the newest's. */
  nested?: boolean;
  chat: AskChat;
  active: boolean;
  renaming: boolean;
  deleting: boolean;
  onRename: (name: string | null) => void;
  onMenu: (what: "rename" | "pin" | "delete") => void;
  onDelete: (sure: boolean) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (renaming) input.current?.select();
  }, [renaming]);
  if (renaming) {
    return (
      <li className="px-1 py-0.5">
        <input
          ref={input}
          defaultValue={chat.title}
          maxLength={120}
          aria-label="Chat name"
          className="h-8 w-full rounded-md border border-border-strong bg-bg px-2 text-[13px] outline-none"
          onKeyDown={(e) => {
            if (e.key === "Enter") onRename(e.currentTarget.value);
            if (e.key === "Escape") onRename(null);
          }}
          onBlur={(e) => onRename(e.currentTarget.value)}
        />
      </li>
    );
  }
  if (deleting) {
    return (
      <li className="rounded-md bg-hover px-2 py-1.5 text-[12px]" role="group" aria-label="Delete this chat">
        <p className="truncate font-medium">{chat.title}</p>
        <p className="text-muted">Delete it from your chats? Your work log keeps the record.</p>
        <div className="mt-1 flex gap-3">
          <button type="button" className="font-medium text-late hover:underline cursor-pointer" onClick={() => onDelete(true)}>
            Delete
          </button>
          <button type="button" className="text-muted hover:text-fg cursor-pointer" onClick={() => onDelete(false)}>
            Cancel
          </button>
        </div>
      </li>
    );
  }
  return (
    <li
      className={cn(
        "group flex items-center rounded-[5px] transition-colors",
        nested && "ml-3 border-l border-border pl-1",
        active ? "bg-nav-on font-medium text-fg" : "text-fg/80 hover:bg-hover hover:text-fg",
      )}
    >
      <Link
        href={`/ask?c=${encodeURIComponent(chat.conversation_id)}`}
        className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-[13px] leading-5"
        aria-current={active ? "page" : undefined}
        title={`${chat.title}\n${chat.questions === 1 ? "1 question" : `${chat.questions} questions`}, last ${formatDateTime(chat.last_at)}`}
      >
        {chat.pinned && <Pin className="size-3 shrink-0 text-accent" strokeWidth={2} aria-label="Pinned" />}
        <span className="min-w-0 flex-1 truncate">{chat.title}</span>
        {chat.running && (
          <span
            className="size-2 shrink-0 rounded-full bg-accent skeleton-shimmer"
            role="status"
            aria-label="Working on an answer"
            title="Working on an answer"
            data-testid="chat-running-dot"
          />
        )}
        {nested && <span className="shrink-0 text-[11px] text-faint tabular-nums">{ago(chat.last_at)}</span>}
      </Link>
      {count ? (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-label={expanded ? `Hide the ${count - 1} earlier times this was asked` : `Asked ${count} times: show the earlier ones`}
          className="mr-1 h-5 shrink-0 rounded-sm px-1.5 text-[11px] text-faint tabular-nums hover:bg-hover hover:text-fg cursor-pointer"
        >
          {count}
        </button>
      ) : null}
      <Menu.Root>
        <Menu.Trigger className="mr-1 hidden size-6 shrink-0 items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-fg group-hover:flex group-focus-within:flex focus-visible:flex data-[state=open]:flex cursor-pointer" aria-label={`More for ${chat.title}`}>
          <Ellipsis className="size-3.5" strokeWidth={2} aria-hidden />
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content align="end" sideOffset={4} className="z-50 min-w-32 rounded-lg border border-border-strong bg-raised p-1.5 shadow-pop">
            <Menu.Item onSelect={() => onMenu("rename")} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover">
              Rename
            </Menu.Item>
            <Menu.Item onSelect={() => onMenu("pin")} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row outline-none data-[highlighted]:bg-hover">
              {chat.pinned ? "Unpin" : "Pin"}
            </Menu.Item>
            <Menu.Item onSelect={() => onMenu("delete")} className="flex cursor-pointer items-center rounded-md px-2 py-1.5 text-row text-late outline-none data-[highlighted]:bg-hover">
              Delete
            </Menu.Item>
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>
    </li>
  );
}
