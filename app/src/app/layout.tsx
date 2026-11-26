import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { Inter, JetBrains_Mono, Plus_Jakarta_Sans } from "next/font/google";
import { AuthProvider } from "@/components/auth";
import { StyleNonce } from "@/components/nonce";
import { ThemeProvider } from "@/components/theme";
import { EARLY_READS_SCRIPT, OS_BOOT_SCRIPT, TICKET_BOOT_SCRIPT } from "@/lib/ticket-boot";
import "./globals.css";

// Plus Jakarta Sans (--font-jakarta -> --font-display): architectural display headings (h1, h2, wordmark).
// Inter (--font-host -> --font-sans): crisp neo-grotesk UI/body text with optical alternates.
// JetBrains Mono (--font-geist-mono -> --font-mono): SQL, code, and IDs.
// Served from the app itself (next/font fetches at build time) so CSP font-src 'self' holds.
const display = Plus_Jakarta_Sans({ subsets: ["latin"], weight: ["500", "600", "700"], variable: "--font-jakarta", display: "swap" });
const sans = Inter({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-host", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], weight: ["400", "500", "600"], variable: "--font-geist-mono", display: "swap" });

export const metadata: Metadata = {
  title: { default: "PLX Workbench", template: "%s · PLX Workbench" },
  description: "Purchase orders, customer orders, suppliers and customers from BC, the Portal, mail, Teams and files.",
  robots: { index: false, follow: false },
  // favicon.ico, icon.svg and apple-icon.png sit beside this file; manifest.ts makes it installable
  applicationName: "PLX Workbench",
  appleWebApp: { capable: true, title: "Workbench", statusBarStyle: "default" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
    { media: "(prefers-color-scheme: dark)", color: "#0f1012" },
  ],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the request makes every page render per request, so each gets the
  // proxy's fresh CSP nonce (src/proxy.ts).
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    <html lang="en-CA" suppressHydrationWarning className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <head>
        {/* Removes an access link's #ticket= before the router records the URL. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: TICKET_BOOT_SCRIPT }} />
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: OS_BOOT_SCRIPT }} />
        {/* The sign-in check's reads, started before the app's scripts load (lib/api.ts takeEarly). */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: EARLY_READS_SCRIPT }} />
      </head>
      <body>
        <svg aria-hidden="true" className="pointer-events-none fixed h-0 w-0 overflow-hidden">
          <defs>
            <filter id="plx-liquid-glass" x="0%" y="0%" width="100%" height="100%" colorInterpolationFilters="sRGB">
              <feTurbulence type="fractalNoise" baseFrequency="0.012 0.018" numOctaves="2" seed="7" result="noise" />
              <feGaussianBlur in="noise" stdDeviation="2.5" result="softNoise" />
              <feDisplacementMap in="SourceGraphic" in2="softNoise" scale="14" xChannelSelector="R" yChannelSelector="G" />
            </filter>
            <filter id="plx-liquid-lens" x="0%" y="0%" width="100%" height="100%" colorInterpolationFilters="sRGB">
              <feTurbulence type="fractalNoise" baseFrequency="0.014 0.02" numOctaves="2" seed="11" result="lensNoise" />
              <feGaussianBlur in="lensNoise" stdDeviation="2" result="softLensNoise" />
              <feDisplacementMap in="SourceGraphic" in2="softLensNoise" scale="8" xChannelSelector="R" yChannelSelector="G" />
            </filter>
          </defs>
        </svg>
        <StyleNonce nonce={nonce} />
        <ThemeProvider nonce={nonce}>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
