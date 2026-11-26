"use client";

import { useParams } from "next/navigation";
import { MeetingFollowUpPanel } from "@/components/meetings/follow-up";
import { Toolbar } from "@/components/ui/toolbar";

// Meetings > a recorded meeting's follow-up: its decisions and action items.
export default function MeetingFollowUpPage() {
  const params = useParams<{ id: string }>();
  return (
    <div className="flex min-h-0 flex-1 flex-col bg-bg">
      <Toolbar title="Meeting follow-up" parent={{ label: "Meetings", href: "/meetings?view=follow-ups" }} />
      <div className="mx-auto w-full max-w-[1160px] px-4 py-5 sm:px-6">
        <MeetingFollowUpPanel meetingId={decodeURIComponent(params.id)} />
      </div>
    </div>
  );
}
