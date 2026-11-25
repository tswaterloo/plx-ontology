import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { Geist_Mono, Host_Grotesk } from "next/font/google";
import { AuthProvider } from "@/components/auth";
import { StyleNonce } from "@/components/nonce";
import { ThemeProvider } from "@/components/theme";
import { EARLY_READS_SCRIPT, OS_BOOT_SCRIPT, TICKET_BOOT_SCRIPT } from "@/lib/ticket-boot";
import "./globals.css";

// Host Grotesk: a crisp grotesque with tabular figures, at home at 12 to 13 px in a dense app.
// The variable font, so Windows can set body text a touch heavier (globals.css). Served from
// the app itself (next/font fetches it at build time), so the CSP's font-src 'self' holds.
// Geist Mono sets the wordmark and every data figure (the num utility). Its Latin subset only,
// fetched and served the same way: the geist package's file holds every script (71 KB on
// every first visit after a deploy); anything outside Latin falls back to the system mono.
const sans = Host_Grotesk({ subsets: ["latin"], variable: "--font-host", display: "swap" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });

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
    { media: "(prefers-color-scheme: light)", color: "#f6f7f9" },
    { media: "(prefers-color-scheme: dark)", color: "#0b0c0e" },
  ],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the request makes every page render per request, so each gets the
  // proxy's fresh CSP nonce (src/proxy.ts).
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    <html lang="en-CA" suppressHydrationWarning className={`${sans.variable} ${mono.variable}`}>
      <head>
        {/* Removes an access link's #ticket= before the router records the URL. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: TICKET_BOOT_SCRIPT }} />
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: OS_BOOT_SCRIPT }} />
        {/* The sign-in check's reads, started before the app's scripts load (lib/api.ts takeEarly). */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: EARLY_READS_SCRIPT }} />
      </head>
      <body>
        <StyleNonce nonce={nonce} />
        <ThemeProvider nonce={nonce}>
          <AuthProvider>{children}</AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
