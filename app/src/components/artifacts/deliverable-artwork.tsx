"use client";

import type { StudioFormatId } from "./artifacts-studio";

/**
 * Editorial-grade dark-mode miniature document previews for the Artifacts Studio
 * "Create a deliverable" showcase strip.
 *
 * Palette discipline: obsidian charcoal (#141519) surface, cool neutral slate ink,
 * subtle specular glass sheen, crisp emerald/sky analytical micro-accents — zero
 * flat solid color blocks and zero purple/violet/indigo tones.
 */
export function DeliverableArtwork({
  id,
  active,
}: {
  id: StudioFormatId;
  active?: boolean;
}) {
  switch (id) {
    case "sheets":
      return <ExcelArtwork active={active} />;
    case "docs":
      return <WordBriefArtwork active={active} />;
    case "slides":
      return <SlideDeckArtwork active={active} />;
    case "charts":
      return <LiveChartArtwork active={active} />;
    case "custom":
      return <QuotePoArtwork active={active} />;
  }
}

function ExcelArtwork({ active }: { active?: boolean }) {
  return (
    <svg
      viewBox="0 0 220 124"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="h-full w-full"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="xl-bg" x1="0" y1="0" x2="220" y2="124" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#1A1D24" />
          <stop offset="55%" stopColor="#141519" />
          <stop offset="100%" stopColor="#101216" />
        </linearGradient>
        <radialGradient id="xl-glow" cx="85%" cy="14%" r="60%">
          <stop offset="0%" stopColor="#10B981" stopOpacity={active ? "0.22" : "0.11"} />
          <stop offset="100%" stopColor="#10B981" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="xl-formula" x1="20" y1="0" x2="200" y2="0" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0.06" />
          <stop offset="50%" stopColor="#34D399" stopOpacity="0.14" />
          <stop offset="100%" stopColor="#FFFFFF" stopOpacity="0.04" />
        </linearGradient>
      </defs>

      <rect width="220" height="124" rx="8" fill="url(#xl-bg)" />
      <rect width="220" height="124" rx="8" fill="url(#xl-glow)" />

      {/* Outer spreadsheet sheet frame */}
      <rect x="12" y="10" width="196" height="104" rx="6" fill="#16181E" stroke="rgba(255,255,255,0.10)" strokeWidth="1" />

      {/* Formula bar */}
      <rect x="12.5" y="10.5" width="195" height="18" rx="5.5" fill="url(#xl-formula)" />
      <line x1="12" y1="28.5" x2="208" y2="28.5" stroke="rgba(255,255,255,0.08)" />
      <text x="20" y="22.5" fill="#6EE7B7" fontSize="7.5" fontWeight="600" fontFamily="ui-monospace, monospace">
        fx
      </text>
      <text x="36" y="22.5" fill="#CBD5E1" fontSize="7" fontFamily="ui-monospace, monospace">
        =SUM(C2:C4)*(1+D2)
      </text>
      <rect x="166" y="14.5" width="35" height="10" rx="3" fill="rgba(16,185,129,0.16)" stroke="rgba(52,211,153,0.35)" strokeWidth="0.7" />
      <text x="171.5" y="21.5" fill="#6EE7B7" fontSize="6.5" fontWeight="600" fontFamily="ui-monospace, monospace">
        +14.2%
      </text>

      {/* Column headers */}
      <rect x="12.5" y="29" width="195" height="13" fill="rgba(255,255,255,0.03)" />
      <line x1="12" y1="42" x2="208" y2="42" stroke="rgba(255,255,255,0.08)" />
      <line x1="68" y1="29" x2="68" y2="100" stroke="rgba(255,255,255,0.06)" />
      <line x1="112" y1="29" x2="112" y2="100" stroke="rgba(255,255,255,0.06)" />
      <line x1="162" y1="29" x2="162" y2="100" stroke="rgba(255,255,255,0.06)" />
      <text x="20" y="38" fill="#64748B" fontSize="6.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">
        PO / SKU
      </text>
      <text x="76" y="38" fill="#64748B" fontSize="6.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">
        UNITS
      </text>
      <text x="122" y="38" fill="#64748B" fontSize="6.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">
        LANDED
      </text>
      <text x="172" y="38" fill="#64748B" fontSize="6.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">
        DELTA
      </text>

      {/* Row 1 */}
      <line x1="12" y1="56.5" x2="208" y2="56.5" stroke="rgba(255,255,255,0.05)" />
      <text x="20" y="52" fill="#E2E8F0" fontSize="7" fontFamily="ui-monospace, monospace">PO-90412</text>
      <text x="76" y="52" fill="#94A3B8" fontSize="7" fontFamily="ui-monospace, monospace">12,400</text>
      <text x="119" y="52" fill="#F1F5F9" fontSize="7" fontFamily="ui-monospace, monospace">$142,800</text>
      <text x="171" y="52" fill="#34D399" fontSize="7" fontFamily="ui-monospace, monospace">+4.2%</text>

      {/* Row 2 */}
      <rect x="12.5" y="57" width="195" height="14" fill="rgba(255,255,255,0.018)" />
      <line x1="12" y1="71" x2="208" y2="71" stroke="rgba(255,255,255,0.05)" />
      <text x="20" y="66.5" fill="#E2E8F0" fontSize="7" fontFamily="ui-monospace, monospace">PO-90438</text>
      <text x="76" y="66.5" fill="#94A3B8" fontSize="7" fontFamily="ui-monospace, monospace">8,200</text>
      <text x="119" y="66.5" fill="#F1F5F9" fontSize="7" fontFamily="ui-monospace, monospace">$98,450</text>
      <text x="171" y="66.5" fill="#34D399" fontSize="7" fontFamily="ui-monospace, monospace">+1.8%</text>

      {/* Row 3 */}
      <line x1="12" y1="85.5" x2="208" y2="85.5" stroke="rgba(255,255,255,0.07)" />
      <text x="20" y="81" fill="#E2E8F0" fontSize="7" fontFamily="ui-monospace, monospace">PO-90451</text>
      <text x="76" y="81" fill="#94A3B8" fontSize="7" fontFamily="ui-monospace, monospace">5,950</text>
      <text x="119" y="81" fill="#F1F5F9" fontSize="7" fontFamily="ui-monospace, monospace">$64,120</text>
      <text x="171" y="81" fill="#FBBF24" fontSize="7" fontFamily="ui-monospace, monospace">-0.6%</text>

      {/* Summary Total Row with active cell highlight */}
      <rect x="12.5" y="86" width="195" height="14" fill="rgba(255,255,255,0.035)" />
      <rect x="112.5" y="86" width="49.5" height="14" fill="rgba(16,185,129,0.14)" stroke="rgba(52,211,153,0.55)" strokeWidth="0.9" />
      <text x="20" y="95.5" fill="#F8FAFC" fontSize="7" fontWeight="600" fontFamily="ui-sans-serif, system-ui">Total</text>
      <text x="76" y="95.5" fill="#CBD5E1" fontSize="7" fontWeight="600" fontFamily="ui-monospace, monospace">26,550</text>
      <text x="118" y="95.5" fill="#FFFFFF" fontSize="7" fontWeight="600" fontFamily="ui-monospace, monospace">$305,370</text>
      <text x="171" y="95.5" fill="#6EE7B7" fontSize="7" fontWeight="600" fontFamily="ui-monospace, monospace">+2.4%</text>

      {/* Bottom sheet tabs */}
      <line x1="12" y1="100" x2="208" y2="100" stroke="rgba(255,255,255,0.08)" />
      <rect x="18" y="102" width="44" height="10" rx="2" fill="rgba(255,255,255,0.08)" />
      <line x1="19" y1="111.5" x2="61" y2="111.5" stroke="#34D399" strokeWidth="1.2" />
      <text x="25" y="109" fill="#E2E8F0" fontSize="6" fontWeight="600" fontFamily="ui-sans-serif, system-ui">Summary</text>
      <text x="70" y="109" fill="#64748B" fontSize="6" fontFamily="ui-sans-serif, system-ui">Landed Cost</text>
      <text x="118" y="109" fill="#64748B" fontSize="6" fontFamily="ui-sans-serif, system-ui">Variance</text>
    </svg>
  );
}

function WordBriefArtwork({ active }: { active?: boolean }) {
  return (
    <svg
      viewBox="0 0 220 124"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="h-full w-full"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="doc-bg" x1="0" y1="0" x2="220" y2="124" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#1A1D24" />
          <stop offset="60%" stopColor="#141519" />
          <stop offset="100%" stopColor="#101216" />
        </linearGradient>
        <radialGradient id="doc-glow" cx="25%" cy="10%" r="65%">
          <stop offset="0%" stopColor="#38BDF8" stopOpacity={active ? "0.18" : "0.09"} />
          <stop offset="100%" stopColor="#38BDF8" stopOpacity="0" />
        </radialGradient>
      </defs>

      <rect width="220" height="124" rx="8" fill="url(#doc-bg)" />
      <rect width="220" height="124" rx="8" fill="url(#doc-glow)" />

      {/* Editorial memo page sheet */}
      <rect x="26" y="8" width="168" height="108" rx="5" fill="#181B22" stroke="rgba(255,255,255,0.11)" strokeWidth="1" />

      {/* Document kicker & top rule */}
      <text x="38" y="21" fill="#7DD3FC" fontSize="6" fontWeight="600" letterSpacing="0.08em" fontFamily="ui-sans-serif, system-ui">
        QUALITY &amp; OPS BRIEF
      </text>
      <text x="155" y="21" fill="#64748B" fontSize="6" fontFamily="ui-monospace, monospace">
        SOP-4.2
      </text>
      <line x1="38" y1="25.5" x2="182" y2="25.5" stroke="rgba(255,255,255,0.09)" />

      {/* Headline & subhead */}
      <rect x="38" y="31" width="108" height="5.5" rx="2" fill="#F1F5F9" fillOpacity="0.90" />
      <rect x="38" y="40" width="76" height="3.2" rx="1.5" fill="#94A3B8" fillOpacity="0.55" />

      {/* Structured callout block with left accent rule */}
      <rect x="38" y="49" width="144" height="22" rx="3" fill="rgba(255,255,255,0.03)" stroke="rgba(255,255,255,0.06)" strokeWidth="0.7" />
      <rect x="38" y="49" width="2.5" height="22" rx="1.2" fill="#38BDF8" />
      <rect x="46" y="54.5" width="112" height="3" rx="1.5" fill="#CBD5E1" fillOpacity="0.72" />
      <rect x="46" y="61" width="90" height="2.8" rx="1.4" fill="#94A3B8" fillOpacity="0.45" />

      {/* Mini specification table preview */}
      <rect x="38" y="77" width="144" height="31" rx="3" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.08)" strokeWidth="0.8" />
      <rect x="38.5" y="77.5" width="143" height="9" rx="2.5" fill="rgba(255,255,255,0.05)" />
      <line x1="38" y1="86.5" x2="182" y2="86.5" stroke="rgba(255,255,255,0.08)" />
      <line x1="38" y1="97" x2="182" y2="97" stroke="rgba(255,255,255,0.05)" />
      <line x1="100" y1="77" x2="100" y2="108" stroke="rgba(255,255,255,0.06)" />
      <line x1="145" y1="77" x2="145" y2="108" stroke="rgba(255,255,255,0.06)" />

      <text x="43" y="83.5" fill="#94A3B8" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">CONTROL</text>
      <text x="105" y="83.5" fill="#94A3B8" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">STATUS</text>
      <text x="150" y="83.5" fill="#94A3B8" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">TARGET</text>

      <rect x="43" y="90.5" width="44" height="2.8" rx="1.4" fill="#CBD5E1" fillOpacity="0.65" />
      <rect x="105" y="89.5" width="24" height="4.8" rx="2" fill="rgba(16,185,129,0.20)" />
      <rect x="150" y="90.5" width="22" height="2.8" rx="1.4" fill="#94A3B8" fillOpacity="0.55" />

      <rect x="43" y="101" width="38" height="2.8" rx="1.4" fill="#CBD5E1" fillOpacity="0.55" />
      <rect x="105" y="100" width="28" height="4.8" rx="2" fill="rgba(56,189,248,0.18)" />
      <rect x="150" y="101" width="18" height="2.8" rx="1.4" fill="#94A3B8" fillOpacity="0.45" />
    </svg>
  );
}

function SlideDeckArtwork({ active }: { active?: boolean }) {
  return (
    <svg
      viewBox="0 0 220 124"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="h-full w-full"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="ppt-bg" x1="0" y1="0" x2="220" y2="124" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#1B1E25" />
          <stop offset="55%" stopColor="#141519" />
          <stop offset="100%" stopColor="#101216" />
        </linearGradient>
        <radialGradient id="ppt-glow" cx="82%" cy="80%" r="65%">
          <stop offset="0%" stopColor="#F59E0B" stopOpacity={active ? "0.18" : "0.09"} />
          <stop offset="100%" stopColor="#F59E0B" stopOpacity="0" />
        </radialGradient>
      </defs>

      <rect width="220" height="124" rx="8" fill="url(#ppt-bg)" />
      <rect width="220" height="124" rx="8" fill="url(#ppt-glow)" />

      {/* Stacked background deck edge for depth */}
      <rect x="18" y="16" width="184" height="96" rx="5" fill="#13151B" stroke="rgba(255,255,255,0.06)" strokeWidth="0.8" />

      {/* Main 16:9 widescreen executive slide */}
      <rect x="12" y="10" width="196" height="100" rx="6" fill="#181B22" stroke="rgba(255,255,255,0.12)" strokeWidth="1" />

      {/* Slide header bar */}
      <rect x="22" y="19" width="90" height="5" rx="2" fill="#F8FAFC" fillOpacity="0.90" />
      <text x="175" y="23.5" fill="#64748B" fontSize="6.5" fontFamily="ui-monospace, monospace">
        01 / 08
      </text>
      <line x1="22" y1="29" x2="198" y2="29" stroke="rgba(255,255,255,0.08)" />

      {/* 3 KPI Stat Tiles */}
      <rect x="22" y="34" width="54" height="25" rx="4" fill="rgba(255,255,255,0.035)" stroke="rgba(255,255,255,0.08)" strokeWidth="0.8" />
      <text x="28" y="43" fill="#64748B" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">ON-TIME</text>
      <text x="28" y="54" fill="#34D399" fontSize="10" fontWeight="700" fontFamily="ui-monospace, monospace">96.4%</text>

      <rect x="83" y="34" width="54" height="25" rx="4" fill="rgba(255,255,255,0.035)" stroke="rgba(255,255,255,0.08)" strokeWidth="0.8" />
      <text x="89" y="43" fill="#64748B" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">OPEN PO</text>
      <text x="89" y="54" fill="#F8FAFC" fontSize="10" fontWeight="700" fontFamily="ui-monospace, monospace">$1.84M</text>

      <rect x="144" y="34" width="54" height="25" rx="4" fill="rgba(255,255,255,0.035)" stroke="rgba(255,255,255,0.08)" strokeWidth="0.8" />
      <text x="150" y="43" fill="#64748B" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">LEAD TIME</text>
      <text x="150" y="54" fill="#FBBF24" fontSize="10" fontWeight="700" fontFamily="ui-monospace, monospace">14.2d</text>

      {/* Bottom Left Mini Bar Chart */}
      <rect x="22" y="65" width="96" height="36" rx="4" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.07)" strokeWidth="0.7" />
      <rect x="31" y="84" width="11" height="12" rx="2" fill="#475569" />
      <rect x="47" y="78" width="11" height="18" rx="2" fill="#64748B" />
      <rect x="63" y="81" width="11" height="15" rx="2" fill="#64748B" />
      <rect x="79" y="73" width="11" height="23" rx="2" fill="#94A3B8" />
      <rect x="95" y="70" width="11" height="26" rx="2" fill="#F59E0B" fillOpacity="0.85" />

      {/* Bottom Right Executive Takeaways */}
      <rect x="125" y="65" width="73" height="36" rx="4" fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.07)" strokeWidth="0.7" />
      <circle cx="133" cy="74.5" r="2" fill="#34D399" />
      <rect x="139" y="73" width="49" height="3" rx="1.5" fill="#CBD5E1" fillOpacity="0.75" />
      <circle cx="133" cy="83.5" r="2" fill="#F59E0B" />
      <rect x="139" y="82" width="42" height="3" rx="1.5" fill="#94A3B8" fillOpacity="0.65" />
      <circle cx="133" cy="92.5" r="2" fill="#38BDF8" />
      <rect x="139" y="91" width="46" height="3" rx="1.5" fill="#94A3B8" fillOpacity="0.55" />
    </svg>
  );
}

function LiveChartArtwork({ active }: { active?: boolean }) {
  return (
    <svg
      viewBox="0 0 220 124"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="h-full w-full"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="ch-bg" x1="0" y1="0" x2="220" y2="124" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#1A1D24" />
          <stop offset="55%" stopColor="#141519" />
          <stop offset="100%" stopColor="#101216" />
        </linearGradient>
        <linearGradient id="ch-bar-hi" x1="0" y1="36" x2="0" y2="102" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#38BDF8" />
          <stop offset="100%" stopColor="#0284C7" stopOpacity="0.35" />
        </linearGradient>
        <linearGradient id="ch-bar-mid" x1="0" y1="50" x2="0" y2="102" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#94A3B8" stopOpacity="0.65" />
          <stop offset="100%" stopColor="#334155" stopOpacity="0.30" />
        </linearGradient>
        <radialGradient id="ch-glow" cx="72%" cy="22%" r="60%">
          <stop offset="0%" stopColor="#38BDF8" stopOpacity={active ? "0.22" : "0.11"} />
          <stop offset="100%" stopColor="#38BDF8" stopOpacity="0" />
        </radialGradient>
      </defs>

      <rect width="220" height="124" rx="8" fill="url(#ch-bg)" />
      <rect width="220" height="124" rx="8" fill="url(#ch-glow)" />

      {/* Chart Frame */}
      <rect x="12" y="10" width="196" height="104" rx="6" fill="#161920" stroke="rgba(255,255,255,0.10)" strokeWidth="1" />

      {/* Top label + trend pill */}
      <text x="22" y="23.5" fill="#E2E8F0" fontSize="7" fontWeight="600" fontFamily="ui-sans-serif, system-ui">
        90-Day Margin &amp; Volume
      </text>
      <rect x="155" y="15" width="44" height="11" rx="3.5" fill="rgba(56,189,248,0.15)" stroke="rgba(56,189,248,0.35)" strokeWidth="0.7" />
      <text x="161" y="22.5" fill="#7DD3FC" fontSize="6.5" fontWeight="600" fontFamily="ui-monospace, monospace">
        +12.6%
      </text>

      {/* Horizontal gridlines */}
      <line x1="22" y1="38" x2="198" y2="38" stroke="rgba(255,255,255,0.06)" strokeDasharray="2 3" />
      <line x1="22" y1="58" x2="198" y2="58" stroke="rgba(255,255,255,0.06)" strokeDasharray="2 3" />
      <line x1="22" y1="78" x2="198" y2="78" stroke="rgba(255,255,255,0.06)" strokeDasharray="2 3" />
      <line x1="22" y1="98" x2="198" y2="98" stroke="rgba(255,255,255,0.10)" />

      {/* Thick rounded analytical bars */}
      <rect x="29" y="72" width="18" height="26" rx="3.5" fill="url(#ch-bar-mid)" />
      <rect x="56" y="61" width="18" height="37" rx="3.5" fill="url(#ch-bar-mid)" />
      <rect x="83" y="66" width="18" height="32" rx="3.5" fill="url(#ch-bar-mid)" />
      <rect x="110" y="50" width="18" height="48" rx="3.5" fill="url(#ch-bar-mid)" />
      <rect x="137" y="44" width="18" height="54" rx="3.5" fill="url(#ch-bar-hi)" />
      <rect x="164" y="36" width="18" height="62" rx="3.5" fill="url(#ch-bar-hi)" />

      {/* Smooth overlaid trend line + data markers */}
      <path
        d="M 38 66 L 65 55 L 92 59 L 119 45 L 146 39 L 173 31"
        stroke="#34D399"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="38" cy="66" r="2.4" fill="#141519" stroke="#34D399" strokeWidth="1.5" />
      <circle cx="65" cy="55" r="2.4" fill="#141519" stroke="#34D399" strokeWidth="1.5" />
      <circle cx="92" cy="59" r="2.4" fill="#141519" stroke="#34D399" strokeWidth="1.5" />
      <circle cx="119" cy="45" r="2.4" fill="#141519" stroke="#34D399" strokeWidth="1.5" />
      <circle cx="146" cy="39" r="2.4" fill="#141519" stroke="#34D399" strokeWidth="1.5" />
      <circle cx="173" cy="31" r="3" fill="#34D399" stroke="#064E3B" strokeWidth="1" />

      {/* Axis labels */}
      <text x="33" y="107.5" fill="#64748B" fontSize="5.8" fontFamily="ui-monospace, monospace">JUN</text>
      <text x="60" y="107.5" fill="#64748B" fontSize="5.8" fontFamily="ui-monospace, monospace">JUL</text>
      <text x="87" y="107.5" fill="#64748B" fontSize="5.8" fontFamily="ui-monospace, monospace">AUG</text>
      <text x="114" y="107.5" fill="#64748B" fontSize="5.8" fontFamily="ui-monospace, monospace">SEP</text>
      <text x="141" y="107.5" fill="#94A3B8" fontSize="5.8" fontFamily="ui-monospace, monospace">OCT</text>
      <text x="168" y="107.5" fill="#E2E8F0" fontSize="5.8" fontWeight="600" fontFamily="ui-monospace, monospace">NOV</text>
    </svg>
  );
}

function QuotePoArtwork({ active }: { active?: boolean }) {
  return (
    <svg
      viewBox="0 0 220 124"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="h-full w-full"
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="po-bg" x1="0" y1="0" x2="220" y2="124" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#1B1D24" />
          <stop offset="55%" stopColor="#141519" />
          <stop offset="100%" stopColor="#101216" />
        </linearGradient>
        <radialGradient id="po-glow" cx="80%" cy="18%" r="60%">
          <stop offset="0%" stopColor="#E2E8F0" stopOpacity={active ? "0.14" : "0.07"} />
          <stop offset="100%" stopColor="#E2E8F0" stopOpacity="0" />
        </radialGradient>
      </defs>

      <rect width="220" height="124" rx="8" fill="url(#po-bg)" />
      <rect width="220" height="124" rx="8" fill="url(#po-glow)" />

      {/* Commercial PO / Customer Quote Sheet */}
      <rect x="20" y="9" width="180" height="106" rx="5.5" fill="#171A21" stroke="rgba(255,255,255,0.11)" strokeWidth="1" />

      {/* Header strip */}
      <text x="30" y="22.5" fill="#F8FAFC" fontSize="7.2" fontWeight="700" fontFamily="ui-monospace, monospace">
        QUOTE #Q-2048
      </text>
      <rect x="155" y="14.5" width="35" height="10.5" rx="3" fill="rgba(255,255,255,0.07)" stroke="rgba(255,255,255,0.14)" strokeWidth="0.7" />
      <text x="160.5" y="21.5" fill="#CBD5E1" fontSize="6" fontWeight="600" fontFamily="ui-monospace, monospace">
        NET 30
      </text>
      <line x1="30" y1="28.5" x2="190" y2="28.5" stroke="rgba(255,255,255,0.08)" />

      {/* Metadata strip */}
      <text x="30" y="37" fill="#64748B" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">CUSTOMER</text>
      <rect x="30" y="40" width="54" height="3.2" rx="1.5" fill="#CBD5E1" fillOpacity="0.75" />
      <text x="122" y="37" fill="#64748B" fontSize="5.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">LEAD TIME</text>
      <text x="122" y="44" fill="#94A3B8" fontSize="6.5" fontFamily="ui-monospace, monospace">3 Weeks FOB</text>

      {/* Line-item header */}
      <rect x="30" y="50" width="160" height="11" rx="2" fill="rgba(255,255,255,0.04)" />
      <text x="35" y="57.5" fill="#94A3B8" fontSize="5.8" fontWeight="600" fontFamily="ui-sans-serif, system-ui">ITEM / SKU</text>
      <text x="112" y="57.5" fill="#94A3B8" fontSize="5.8" fontWeight="600" fontFamily="ui-sans-serif, system-ui">QTY</text>
      <text x="152" y="57.5" fill="#94A3B8" fontSize="5.8" fontWeight="600" fontFamily="ui-sans-serif, system-ui">AMOUNT</text>

      {/* Line items */}
      <line x1="30" y1="73" x2="190" y2="73" stroke="rgba(255,255,255,0.05)" />
      <text x="35" y="69" fill="#E2E8F0" fontSize="6.5" fontFamily="ui-monospace, monospace">SKU-8840 Pump</text>
      <text x="112" y="69" fill="#94A3B8" fontSize="6.5" fontFamily="ui-monospace, monospace">12,000</text>
      <text x="151" y="69" fill="#F1F5F9" fontSize="6.5" fontFamily="ui-monospace, monospace">$28,400</text>

      <line x1="30" y1="85.5" x2="190" y2="85.5" stroke="rgba(255,255,255,0.05)" />
      <text x="35" y="81.5" fill="#E2E8F0" fontSize="6.5" fontFamily="ui-monospace, monospace">SKU-4120 Bottle</text>
      <text x="112" y="81.5" fill="#94A3B8" fontSize="6.5" fontFamily="ui-monospace, monospace">18,500</text>
      <text x="151" y="81.5" fill="#F1F5F9" fontSize="6.5" fontFamily="ui-monospace, monospace">$20,520</text>

      {/* Bold total rule */}
      <line x1="30" y1="94" x2="190" y2="94" stroke="rgba(255,255,255,0.16)" strokeWidth="1" />
      <text x="35" y="105" fill="#94A3B8" fontSize="6.5" fontWeight="600" fontFamily="ui-sans-serif, system-ui">
        TOTAL USD
      </text>
      <text x="142" y="105.5" fill="#6EE7B7" fontSize="8.5" fontWeight="700" fontFamily="ui-monospace, monospace">
        $48,920.00
      </text>
    </svg>
  );
}
