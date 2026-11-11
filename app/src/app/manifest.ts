import type { MetadataRoute } from "next";

// Installable: Chrome and Edge (Windows and Mac) offer "Install PLX Workbench", and it opens in
// its own window with this name, icon and title-bar colour. Every icon is the cube mark (cyan,
// blue and navy faces, no tile); the maskable one sets it on the dark field inside Android's
// safe zone. The 16 and 32 px favicons are the hand-checked small renders from the concept sheet.
export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/",
    name: "PLX Workbench",
    short_name: "Workbench",
    description: "Purchase orders, customer orders, lots, suppliers and customers, with sources.",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#ffffff",
    theme_color: "#f5f6f8",
    icons: [
      { src: "/icons/icon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/icons/icon-96.png", sizes: "96x96", type: "image/png" },
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
