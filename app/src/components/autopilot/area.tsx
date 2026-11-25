"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { EveryoneView } from "@/components/autopilot/prepared";
import { DraftsPanel } from "@/components/autopilot/opt-in";
import { TeamsConnect } from "@/components/autopilot/teams-connect";
import { ErrorNotice } from "@/components/ui/notice";
import type { AutopilotArea as Area } from "@/lib/types";

// The Switches tab of Home (route /autopilot): what to prepare for you, per kind of work,
// off, confirm each one, or Auto where it was earned, with your own limits on Auto. The
// items themselves are on Home's queue; a link to one (?item=) goes there. Admins also see
// who switched what on and how each kind is doing. Nothing is sent until you confirm it.

export function AutopilotView({ area: initial }: { area: Area; onReload?: () => void }) {
  const [area, setArea] = useState(initial);
  useEffect(() => setArea(initial), [initial]);
  const params = useSearchParams();
  const router = useRouter();
  const linked = params.get("item");
  useEffect(() => {
    if (params.get("tab") === "audit") router.replace("/assist?show=log");
    else if (linked) router.replace(`/?item=${encodeURIComponent(linked)}`);
  }, [params, router, linked]);
  const admin = area.owner || area.drafts_switched_on != null;
  return (
    <div className="space-y-6" data-testid="autopilot">
      {area.switches.global_off && <ErrorNotice tone="notice" message="Every autopilot write is switched off. A confirm is recorded and nothing is sent." />}
      {area.switches.autopilot_off && <ErrorNotice tone="notice" message="The autopilot is switched off on this server: nothing new is prepared." />}
      <DraftsPanel area={area} onArea={setArea} />
      <TeamsConnect />
      {admin && (
        <section aria-labelledby="ap-everyone" className="space-y-3">
          <h2 id="ap-everyone" className="text-[13px] font-semibold">
            Everyone
          </h2>
          <EveryoneView area={area} />
        </section>
      )}
    </div>
  );
}
