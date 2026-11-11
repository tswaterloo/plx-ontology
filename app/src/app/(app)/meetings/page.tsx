"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Upcoming } from "@/components/meetings/upcoming";
import { WeeklyReview } from "@/components/meetings/weekly";
import { MyActionItemsPanel } from "@/components/meetings/follow-up";
import { Toolbar } from "@/components/ui/toolbar";
import { cn } from "@/lib/cn";

const VIEWS = [
  { view: "upcoming", label: "Upcoming" },
  { view: "weekly", label: "Weekly review" },
  { view: "follow-ups", label: "Follow-ups" },
] as const;

/** Follow-ups: your action items from recorded meetings (the meeting autopilot's), each
 * linking to its meeting's decisions; ?person= shows another person's for meetings you
 * organised. */
function FollowUps({ person }: { person: string | null }) {
  return <MyActionItemsPanel person={person} />;
}

function Meetings() {
  const params = useSearchParams();
  const view = VIEWS.find((v) => v.view === params.get("view"))?.view ?? "upcoming";
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar heading title="Meetings" />
      <nav className="flex h-10 shrink-0 items-stretch gap-4 border-b border-border px-5" aria-label="Meetings" role="tablist">
        {VIEWS.map((v) => (
          <Link
            key={v.view}
            href={v.view === "upcoming" ? "/meetings" : `/meetings?view=${v.view}`}
            role="tab"
            aria-selected={view === v.view}
            className={cn(
              "relative flex items-center text-[13px]",
              view === v.view ? "font-medium text-fg after:absolute after:inset-x-0 after:-bottom-px after:h-[2px] after:bg-accent" : "text-muted hover:text-fg",
            )}
          >
            {v.label}
          </Link>
        ))}
      </nav>
      <div className="p-5" data-testid={`meetings-${view}`}>
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
