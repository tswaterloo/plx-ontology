"use client";

// Your own Teams sign-in: once connected, a Teams reply you confirm posts as you.

import { StoredAge } from "@/components/ui/stored-age";
import { useRead } from "@/lib/use-read";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { ErrorNotice } from "@/components/ui/notice";
import { forgetTeams, getTeamsConnection, startTeamsConnect, type TeamsConnection } from "@/lib/api";
import { plainError } from "@/lib/errors";

const WHY: Record<string, string> = {
  declined: "You did not finish the Microsoft sign-in.",
  another_account: "You signed in with another account than yours. Sign in as yourself.",
  scope_missing: "Microsoft did not grant sending Teams messages. An admin has to allow it first.",
  state_expired: "The sign-in took too long. Start it again.",
  state_unknown: "That sign-in link was already used or is not from this Workbench. Start it again.",
};

export function TeamsConnect() {
  const params = useSearchParams();
  const { data: state, set: setState, at } = useRead<TeamsConnection>("/api/teams/connection", getTeamsConnection);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const failed = params?.get("teams") === "failed" ? WHY[params.get("why") ?? ""] ?? "The Teams sign-in did not complete." : null;
  if (!state) return null;
  const connect = async () => {
    setBusy(true);
    setError(null);
    try {
      window.location.assign((await startTeamsConnect()).url);
    } catch (e) {
      setError(plainError(e));
      setBusy(false);
    }
  };
  const forget = async () => {
    setBusy(true);
    try {
      setState(await forgetTeams());
    } catch (e) {
      setError(plainError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-3 text-[13px]" data-testid="teams-connect">
      <StoredAge at={at} />
      {state.connected ? (
        <>
          <span>Teams replies you confirm post as you. A posted reply cannot be taken back from here.</span>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void forget()}>
            Disconnect Teams
          </Button>
        </>
      ) : (state.missing ?? []).length > 0 ? (
        <span className="text-muted">Teams replies can be copied; posting them as you is not set up on this server yet.</span>
      ) : (
        <>
          <span className="text-muted">Sign in to Teams once so the replies you confirm post as you.</span>
          <Button size="sm" disabled={busy} onClick={() => void connect()}>
            Connect Teams
          </Button>
        </>
      )}
      {failed && <ErrorNotice message={failed} />}
      {error && <ErrorNotice message={error} />}
    </div>
  );
}
