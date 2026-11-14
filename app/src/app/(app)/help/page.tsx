"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useAuth } from "@/components/auth";
import { OnboardingChecklist } from "@/components/onboarding-checklist";
import { RoleGuide } from "@/components/role-guide";
import { Toolbar } from "@/components/ui/toolbar";
import { cn } from "@/lib/cn";
import { GUIDES, guideFor, guideLabel, roleKey, type GuideRole } from "@/lib/onboarding";

// Help: the first-week guide per role (what the Workbench does for you, how to ask, how
// autopilot opt-in works, how to give feedback) with the week-one checklist. The role comes
// from ?role=, else the one this reader picked last time in this browser.
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
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Toolbar title="Help" heading />
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
