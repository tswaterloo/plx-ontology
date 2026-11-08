"use client";

import { useSearchParams } from "next/navigation";
import { MyActionItemsPanel } from "@/components/meetings/follow-up";

// Meetings > My action items (?person= another person's, for meetings you organised).
export default function MyActionItemsPage() {
  const person = useSearchParams().get("person");
  return <MyActionItemsPanel person={person} />;
}
