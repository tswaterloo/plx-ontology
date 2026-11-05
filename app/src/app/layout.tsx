import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { Public_Sans } from "next/font/google";
import { AuthProvider } from "@/components/auth";
import { StyleNonce } from "@/components/nonce";
import { ThemeProvider } from "@/components/theme";
import { TICKET_BOOT_SCRIPT } from "@/lib/ticket-boot";
import "./globals.css";

// Public Sans (US Web Design System): a neutral face drawn for data, with tabular figures.
// Served from the app itself (next/font fetches it at build time), so the CSP's font-src 'self' holds.
const sans = Public_Sans({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-public-sans", display: "swap" });

export const metadata: Metadata = {
  title: { default: "PLX Workbench", template: "%s · PLX Workbench" },
  description: "Purchase orders, sales orders, suppliers and customers from BC, the Portal, mail, Teams and files.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#161616" },
  ],
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the request makes every page render per request, so each gets the
  // proxy's fresh CSP nonce (src/proxy.ts).
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    <html lang="en-CA" suppressHydrationWarning className={sans.variable}>
      <head>
        {/* Removes an access link's #ticket= before the router records the URL. */}
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: TICKET_BOOT_SCRIPT }} />
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
