"use client";

import { useParams } from "next/navigation";
import { MeetingFollowUpPanel } from "@/components/meetings/follow-up";

// Meetings > a recorded meeting's follow-up.
export default function MeetingFollowUpPage() {
  const params = useParams<{ id: string }>();
  return <MeetingFollowUpPanel meetingId={decodeURIComponent(params.id)} />;
}
