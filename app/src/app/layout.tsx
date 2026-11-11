import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { Host_Grotesk } from "next/font/google";
import { GeistMono } from "geist/font/mono";
import { AuthProvider } from "@/components/auth";
import { StyleNonce } from "@/components/nonce";
import { ThemeProvider } from "@/components/theme";
import { OS_BOOT_SCRIPT, TICKET_BOOT_SCRIPT } from "@/lib/ticket-boot";
import "./globals.css";

// Host Grotesk: a crisp grotesque with tabular figures, at home at 12 to 13 px in a dense app.
// The variable font, so Windows can set body text a touch heavier (globals.css). Served from
// the app itself (next/font fetches it at build time), so the CSP's font-src 'self' holds.
// Geist Mono (bundled with the geist package, served locally) sets only the PLX of the wordmark.
const sans = Host_Grotesk({ subsets: ["latin"], variable: "--font-host", display: "swap" });

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
    { media: "(prefers-color-scheme: light)", color: "#f5f6f8" },
    { media: "(prefers-color-scheme: dark)", color: "#1c1f24" },
  ],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the request makes every page render per request, so each gets the
  // proxy's fresh CSP nonce (src/proxy.ts).
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    <html lang="en-CA" suppressHydrationWarning className={`${sans.variable} ${GeistMono.variable}`}>
      <head>
        {/* Removes an access link's #ticket= before the router records the URL. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: TICKET_BOOT_SCRIPT }} />
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: OS_BOOT_SCRIPT }} />
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
