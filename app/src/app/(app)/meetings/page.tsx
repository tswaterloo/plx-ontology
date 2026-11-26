"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Upcoming } from "@/components/meetings/upcoming";
import { WeeklyReview } from "@/components/meetings/weekly";
import { MyActionItemsPanel } from "@/components/meetings/follow-up";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { cn } from "@/lib/cn";

const VIEWS = [
  { view: "upcoming", label: "Upcoming" },
  { view: "weekly", label: "Weekly review" },
  { view: "follow-ups", label: "Follow-ups" },
] as const;

function FollowUps({ person }: { person: string | null }) {
  return <MyActionItemsPanel person={person} />;
}

function Meetings() {
  const params = useSearchParams();
  const view = VIEWS.find((v) => v.view === params.get("view"))?.view ?? "upcoming";
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar heading title="Meetings" />
      <nav
        className="flex h-10 shrink-0 items-center border-b border-border bg-bg"
        aria-label="Meetings"
        role="tablist"
      >
        <div className="mx-auto flex h-full w-full max-w-[1360px] items-stretch gap-5 overflow-x-auto px-4 [scrollbar-width:none] sm:px-6 [&::-webkit-scrollbar]:hidden">
          {VIEWS.map((v) => (
            <Link
              key={v.view}
              href={v.view === "upcoming" ? "/meetings" : `/meetings?view=${v.view}`}
              role="tab"
              aria-selected={view === v.view}
              className={cn(
                "relative flex shrink-0 items-center whitespace-nowrap text-[13px] transition-colors duration-150",
                view === v.view
                  ? "font-medium text-fg after:absolute after:inset-x-0 after:bottom-0 after:h-[2px] after:bg-fg"
                  : "text-muted hover:text-fg",
              )}
            >
              {v.label}
            </Link>
          ))}
        </div>
      </nav>
      <PageIntro path="/meetings" className="mx-auto w-full max-w-[1360px] px-4 sm:px-6 lg:px-6" />
      <div className="mx-auto w-full max-w-[1360px] px-4 py-5 sm:px-6" data-testid={`meetings-${view}`}>
        {view === "weekly" ? <WeeklyReview /> : view === "follow-ups" ? <FollowUps person={params.get("person")} /> : <Upcoming />}
      </div>
    </div>
  );
}

export default function MeetingsPage() {
  return (
    <Suspense>
      <Meetings />
    </Suspense>
  );
}
