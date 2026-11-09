"use client";

import { useParams } from "next/navigation";
import { MeetingFollowUpPanel } from "@/components/meetings/follow-up";
import { Toolbar } from "@/components/ui/toolbar";

// Meetings > a recorded meeting's follow-up: its decisions and action items.
export default function MeetingFollowUpPage() {
  const params = useParams<{ id: string }>();
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar title="Meeting follow-up" parent={{ label: "Meetings", href: "/meetings?view=follow-ups" }} />
      <div className="p-5">
        <MeetingFollowUpPanel meetingId={decodeURIComponent(params.id)} />
      </div>
    </div>
  );
}
