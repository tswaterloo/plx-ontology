# PLX Workbench desktop app

The same Workbench as the browser, in its own window for Windows, Mac and Linux. It is a thin
Electron shell: the pages, the sign-in and every permission come from the Workbench server, as for
a browser user. Nothing is cached or answered in the app.

What the shell adds: its own window, icon and menu; system notifications for a new reliability
alarm and for an Autopilot item waiting on you (built from reads the page already makes, as the
signed-in reader); Help > Give feedback (the same in-app sheet); updates from our own server.

## Run it

    cd desktop && npm install
    PLX_WORKBENCH_URL=https://<host> npm start       # or leave it unset: the first run asks once

`https://` only (`http://` for localhost). The first run asks for the address and checks that a
Workbench answers there (its public `/api/health`) before keeping it in the app's user-data folder;
File > Change server changes it. Sign-in is the Workbench's own (Microsoft redirect inside the window).
A company whose Microsoft sign-in passes through its own identity server (ADFS or similar) names that
host in `PLX_DESKTOP_SIGNIN_HOSTS` (exact names, comma-separated); any other host is opened in the browser.

## Build

    npm run pack          # unpacked app, any platform's files (no installer)
    npm run dist:linux    # AppImage + tar.gz   (built and checked on the dev host)
    npm run dist:win      # NSIS installer + zip: on Windows, or on Linux with wine (64- and 32-bit)
    npm run dist:win:zip  # the Windows app as a zip
    npm run dist:mac      # dmg + zip, Intel and Apple Silicon: run on a Mac (the dmg needs macOS)
    npm run dist:mac:zip  # the Mac app as zips (Intel and Apple Silicon), unsigned: any host

Outputs go to `dist/` (ignored by git). All builds are UNSIGNED. Built on a Linux cloud host (11-13,
with wine and wine32 installed): AppImage, tar.gz, the Windows NSIS installer and zip (icon and
version info written into the .exe), and both Mac zips; the packaged Linux app passed the smoke test.
Not built there: the Mac dmg (needs macOS; CI builds it).

Published 11-15 (unsigned 0.1.0, built on the host with wine + wine32 for the NSIS uninstaller step):
the Windows installer and zip and both Mac zips, with `latest.yml` / `latest-mac.yml` and an
`index.html` download page, in `/data/plx-desktop-feed` (served at `https://<host>/desktop/`, the
Help page's Download link). Each file is copied in as a temporary name and renamed, owned by caddy.
For a new version: bump `version` in package.json, build with `-c.publish.url=https://<host>/desktop/`,
copy the installers and blockmaps first and the two `latest*.yml` last (an app that reads a new
`latest.yml` must find its installer), then update `index.html`.

## CI

`.github/workflows/desktop.yml` runs on a push or pull request touching `desktop/` (never on main):
the tests, the smoke test from the sources and of the packaged Linux app, and unsigned installers on
Linux, Windows and macOS runners, uploaded as build artifacts (14 days). Nothing is published.
By hand (Actions > desktop > Run workflow): `feed_url` builds for an update feed, and `signed`
signs the Windows and Mac builds from repository secrets (the variables below, plus
`PLX_MAC_CERT_P12_BASE64` / `PLX_MAC_CERT_PASSWORD` for the Developer ID certificate); a missing
secret stops the signed build before it starts. `test/workflow.test.js` keeps the workflow in step
with `package.json` and the signing preflight.

## Signing (waits on Vince)

Ready to switch on: `electron-builder.signed.yml` extends the plain config with signing read from
the environment, and `npm run dist:win:signed` / `dist:mac:signed` first run `scripts/check-signing.js`,
which names each missing variable and builds nothing when one is absent. Windows needs
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `PLX_WIN_PUBLISHER_NAME`,
`PLX_WIN_SIGN_ENDPOINT`, `PLX_WIN_SIGN_ACCOUNT`, `PLX_WIN_SIGN_PROFILE`; the Mac build (on a Mac) needs
`PLX_MAC_IDENTITY`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`. The plain config stays
unsigned. Not run with real accounts (none exist yet): live-unverified. Details:

- Windows: Azure Trusted Signing (preferred) or an Authenticode certificate. Add `azureSignOptions`
  to `electron-builder.yml` (or set `CSC_LINK` / `CSC_KEY_PASSWORD`) and drop `signExecutable: false`.
  Until then Windows shows SmartScreen's "unknown publisher".
- macOS: an Apple Developer account (Developer ID Application certificate + notarization with an
  app-specific password). Set `mac.identity`, `hardenedRuntime: true` and `notarize`. Until then the
  app opens only via right-click > Open, and macOS cannot auto-update an unsigned app.

## Updates

`electron-updater` reads a generic feed at the URL in `electron-builder.yml` `publish.url`
(`https://<host>/desktop/`, a folder our server serves holding the installers and the generated
`latest*.yml`). Build a release with `npm run dist:linux -- -c.publish.url=https://<host>/desktop/`
and copy `dist/*` there. deploy/Caddyfile serves `/desktop/` from `PLX_DESKTOP_FEED` (default `/data/plx-desktop-feed`, readable by the caddy user; copy the build output there); until the folder holds a build the app runs and never updates.
The app checks at start and on Help > Check for updates (which says what it found); a downloaded
update installs when the app is quit. A missing feed is silent at start. Set `PLX_DESKTOP_NO_UPDATE=1`
to turn the check off.

## Tests

    npm test                                  # address, navigation, notification, signing and CI rules (node:test)
    PLX_DESKTOP_SMOKE=1 npm test              # also starts the real shell under xvfb-run
    PLX_DESKTOP_SMOKE=1 PLX_DESKTOP_BINARY=dist/linux-unpacked/plx-workbench-desktop npm test   # the packaged app

The smoke test (needs the GTK libraries Electron links) checks the page gets the bridge and no Node,
and that a server redirect off the Workbench is not followed; it passed on the Linux cloud host on
11-13, from the sources and packaged. Live-unverified: notifications, the menu, the first-run probe
against a real server, sign-in, and an update actually installing (needs a served feed).

## Security

Context isolation and sandbox on, no Node in the page, the one bridge in `src/preload.js`; the window
only navigates (by click or by server redirect) to the Workbench origin and Microsoft's sign-in pages,
web or mail links open in the person's own browser, no `<webview>` may attach, and the only local file
it shows is the first-run page (the one page that may set the address); every page permission is denied; only the Workbench's own pages may ask for a
notification (a few per minute at most, links only inside the Workbench).
