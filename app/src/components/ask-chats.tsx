"use client";

import * as Menu from "@radix-ui/react-dropdown-menu";
import { Ellipsis, Pin, Search, SquarePen } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { getAskChats, updateAskChat } from "@/lib/api";
import { cn } from "@/lib/cn";
import { plainError } from "@/lib/errors";
import { ago, formatDateTime } from "@/lib/format";
import type { AskChat, AskChats } from "@/lib/types";

/** Chats drawn at first; the rest are one click away (Show more, Show all). */
const CHATS_FIRST = 15;

/**
 * Your Ask conversations, like a chat app's list: pinned first, then the most recent, each
 * titled by its first question unless you named it. Open one to read it and go on asking in
 * it; rename, pin or delete it from its menu; search names and questions. Deleting takes it
 * off this list only: your work log keeps the record of what was asked.
 */
export function AskChatList({ current, refreshKey, onNew, className }: { current?: string | null; refreshKey?: unknown; onNew: () => void; className?: string }) {
  const [q, setQ] = useState("");
  const [list, setList] = useState<AskChats | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [drawn, setDrawn] = useState(CHATS_FIRST);

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

  const chats = list?.chats ?? [];
  const pinned = chats.filter((c) => c.pinned);
  const rest = chats.filter((c) => !c.pinned);
  const row = (chat: AskChat) => (
    <ChatRow
      key={chat.conversation_id}
      chat={chat}
      active={chat.conversation_id === current}
      renaming={renaming === chat.conversation_id}
      deleting={deleting === chat.conversation_id}
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

  return (
    <nav className={cn("flex min-h-0 flex-col", className)} aria-label="Chats" data-testid="ask-chats">
      <div className="flex items-center justify-between gap-2 px-3 pt-3 pb-2">
        <h2 className="text-[13px] font-semibold">Chats</h2>
        <button type="button" onClick={onNew} className="flex h-7 items-center gap-1.5 rounded-md px-2 text-[13px] text-muted hover:bg-hover hover:text-fg cursor-pointer" data-testid="new-chat">
          <SquarePen className="size-3.5" strokeWidth={2} aria-hidden />
          New chat
        </button>
      </div>
      <label className="mx-3 mb-2 flex h-8 items-center gap-2 rounded-md border border-border px-2 text-[13px] focus-within:border-border-strong">
        <Search className="size-3.5 shrink-0 text-muted" strokeWidth={2} aria-hidden />
        <input value={q} onChange={(e) => setQ(e.target.value.slice(0, 200))} placeholder="Search chats" aria-label="Search chats" className="min-w-0 flex-1 bg-transparent outline-none placeholder:text-faint" />
      </label>
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-3">
        {problem && <p className="px-2 py-1 text-[12px] text-late" role="alert">{problem}</p>}
        {list && !list.available && <p className="px-2 py-1 text-[12px] text-muted">Your chats can't be listed just now.</p>}
        {list && list.available && chats.length === 0 && <p className="px-2 py-1 text-[12px] text-muted">{q ? "No chat matches that." : "Your questions will be listed here."}</p>}
        {pinned.length > 0 && (
          <>
            <h3 className="px-2 pt-1 pb-1 text-[11px] font-medium text-faint">Pinned</h3>
            <ul>{pinned.map(row)}</ul>
          </>
        )}
        {rest.length > 0 && (
          <>
            {pinned.length > 0 && <h3 className="px-2 pt-3 pb-1 text-[11px] font-medium text-faint">Recent</h3>}
            <ul>{rest.slice(0, drawn).map(row)}</ul>
            {rest.length > drawn && (
              <p className="flex items-center gap-3 px-2 py-1.5 text-[12px] text-muted" data-testid="more-chats">
                {rest.length - drawn} more
                <button type="button" className="cursor-pointer text-accent hover:underline" onClick={() => setDrawn(drawn + 30)}>Show 30 more</button>
                <button type="button" className="cursor-pointer text-accent hover:underline" onClick={() => setDrawn(rest.length)}>Show all</button>
              </p>
            )}
          </>
        )}
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
}: {
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
    <li className={cn("group flex items-center rounded-md", active ? "bg-hover" : "hover:bg-hover")}>
      <Link
        href={`/ask?c=${encodeURIComponent(chat.conversation_id)}`}
        className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-[13px]"
        aria-current={active ? "page" : undefined}
        title={`${chat.title}\n${chat.questions === 1 ? "1 question" : `${chat.questions} questions`}, last ${formatDateTime(chat.last_at)}`}
      >
        {chat.pinned && <Pin className="size-3 shrink-0 text-faint" strokeWidth={2} aria-label="Pinned" />}
        <span className="min-w-0 flex-1 truncate">{chat.title}</span>
        <span className="shrink-0 text-[11px] text-faint tabular-nums group-hover:hidden">{ago(chat.last_at)}</span>
      </Link>
      <Menu.Root>
        <Menu.Trigger className="mr-1 hidden size-6 shrink-0 items-center justify-center rounded text-muted hover:text-fg group-hover:flex group-focus-within:flex focus-visible:flex data-[state=open]:flex cursor-pointer" aria-label={`More for ${chat.title}`}>
          <Ellipsis className="size-4" strokeWidth={2} aria-hidden />
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content align="end" sideOffset={4} className="z-50 min-w-32 rounded-xl bg-raised p-1.5 shadow-pop">
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
