import type { MetadataRoute } from "next";

// Installable: Chrome and Edge (Windows and Mac) offer "Install PLX Workbench", and it opens in
// its own window with this name, icon and title-bar colour. The "any" icons are the rounded tile
// on a clear ground (the Windows taskbar and Start menu, the macOS Dock); the maskable one is
// full-bleed for Android, which cuts its own shape.
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
    theme_color: "#f6f6f5",
    icons: [
      { src: "/icons/icon.svg", sizes: "any", type: "image/svg+xml" },
      { src: "/icons/icon-96.png", sizes: "96x96", type: "image/png" },
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
