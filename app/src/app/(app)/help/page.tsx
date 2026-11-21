"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useAuth } from "@/components/auth";
import { OnboardingChecklist } from "@/components/onboarding-checklist";
import { RoleGuide } from "@/components/role-guide";
import { Toolbar } from "@/components/ui/toolbar";
import { PageIntro } from "@/components/ui/page-intro";
import { cn } from "@/lib/cn";
import { desktopBridge } from "@/lib/desktop";
import { GUIDES, guideFor, guideLabel, roleKey, type GuideRole } from "@/lib/onboarding";

// Help: the first-week guide per role (what the Workbench does for you, how to ask, how
// autopilot opt-in works, how to give feedback) with the week-one checklist. The role comes
// from ?role=, else the one this reader picked last time in this browser.
/** What a person can tell the Ask to do, one line each, in the words they would type. */
const TELL = [
  "From now on, keep answers short. Memory keeps it; correct or forget it there.",
  "Stop drafting replies to news@supplier.com. Always cc ana@company.com on supplier emails. The Autopilot follows both.",
  "Save a template for chasing a late PO. Use my late PO template for the oldest late PO.",
  "Write a one-page memo of our late POs and email it to Ana as a Word file.",
  "Make that a Word document and save it beside the March price list in SharePoint.",
  "Rewrite this in my style: ...",
];

export default function HelpPage() {
  return (
    <Suspense>
      <Help />
    </Suspense>
  );
}

function Help() {
  const { state } = useAuth();
  const principal = state.status === "signed-in" ? state.me.principal : null;
  const router = useRouter();
  const asked = useSearchParams().get("role");
  const [role, setRole] = useState<GuideRole | null>(null);
  useEffect(() => {
    if (asked && guideFor(asked)) return setRole(asked as GuideRole);
    if (!principal) return;
    try {
      const kept = window.localStorage.getItem(roleKey(principal));
      if (kept && guideFor(kept)) setRole(kept as GuideRole);
    } catch {
      /* private mode: pick again */
    }
  }, [asked, principal]);
  const pick = (r: GuideRole) => {
    setRole(r);
    if (principal) {
      try {
        window.localStorage.setItem(roleKey(principal), r);
      } catch {
        /* private mode */
      }
    }
    router.replace(`/help?role=${r}`, { scroll: false });
  };
  const guide = role ? guideFor(role) : null;
  // the download is offered in the browser only; inside the desktop app it is already installed
  const [inBrowser, setInBrowser] = useState(false);
  useEffect(() => setInBrowser(desktopBridge() === null), []);
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar title="Help" heading />
      <PageIntro path="/help" />
      <div className="mx-auto w-full max-w-[52rem] space-y-6 px-5 py-5">
        <section aria-labelledby="help-role" className="space-y-2">
          <h2 id="help-role" className="text-[13px] font-semibold">
            Your role
          </h2>
          <p className="text-[12px] text-muted">Pick the work you do most. You can switch to read another role&apos;s guide at any time.</p>
          <div role="radiogroup" aria-labelledby="help-role" className="flex flex-wrap gap-1.5">
            {GUIDES.map((g) => (
              <button
                key={g.role}
                type="button"
                role="radio"
                aria-checked={role === g.role}
                onClick={() => pick(g.role)}
                className={cn(
                  "h-7 rounded-md border border-border px-2.5 text-[12px] cursor-pointer",
                  role === g.role ? "bg-[color-mix(in_srgb,var(--fg)_8%,transparent)] font-medium text-fg" : "text-muted hover:bg-hover hover:text-fg",
                )}
                data-testid={`help-role-${g.role}`}
              >
                {guideLabel(g.role)}
              </button>
            ))}
          </div>
        </section>
        {principal && <OnboardingChecklist principal={principal} role={role} />}
        <section aria-labelledby="help-tell" className="space-y-1" data-testid="help-tell">
          <h2 id="help-tell" className="text-[13px] font-semibold">
            Tell it, not only ask it
          </h2>
          <p className="text-[12px] text-muted">Type these in the Ask box, in your own words. Nothing is sent or saved anywhere until you confirm it.</p>
          <ul className="list-disc space-y-0.5 pl-5 text-[12px]">
            {TELL.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </section>
        {inBrowser && (
          <section aria-labelledby="help-desktop" className="space-y-1" data-testid="help-desktop">
            <h2 id="help-desktop" className="text-[13px] font-semibold">
              Desktop app
            </h2>
            <p className="text-[12px] text-muted">
              The same Workbench in its own window for Windows and Mac, with notifications when something is waiting on you.{" "}
              <a href="/desktop/" className="text-fg underline underline-offset-2" data-testid="help-desktop-download">
                Download the desktop app
              </a>
              . The installers are not signed yet: on Windows choose More info, then Run anyway; on a Mac right-click the app and choose Open.
            </p>
          </section>
        )}
        {guide ? (
          <RoleGuide guide={guide} />
        ) : (
          <p className="text-[12px] text-muted" data-testid="help-pick">
            Pick a role above to see what the Workbench does for it.
          </p>
        )}
      </div>
    </div>
  );
}
