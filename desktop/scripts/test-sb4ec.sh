#!/bin/sh
# Run the desktop tests on sb4ec, where there is no system Xvfb and no sudo:
# unpack the Ubuntu xvfb package in the person's home, start it, then run the real Electron tests.
# Copies nothing secret; the package is Ubuntu's public xvfb build.
set -eu
ROOT=$HOME/xvfb-user
LISTS=$HOME/apt-user/lists
CACHE=$HOME/apt-user/cache
if [ ! -x "$ROOT/root/usr/bin/Xvfb" ]; then
  mkdir -p "$ROOT" "$LISTS/partial" "$CACHE/archives/partial"
  apt-get -o Dir::State::Lists="$LISTS" -o Dir::Cache="$CACHE" update >/dev/null
  (cd "$ROOT" && apt-get -o Dir::State::Lists="$LISTS" -o Dir::Cache="$CACHE" download xvfb >/dev/null)
  mkdir -p "$ROOT/root"
  (cd "$ROOT" && dpkg-deb -x xvfb_*.deb root)
fi
mkdir -p "$HOME/bin"
cat > "$HOME/bin/xvfb-run" <<'EOS'
#!/bin/sh
[ "$1" = -a ] && shift
exec env DISPLAY=:99 "$@"
EOS
chmod +x "$HOME/bin/xvfb-run"
[ -d node_modules/electron ] || npm ci --no-audit --no-fund
[ -x node_modules/electron/dist/electron ] || node node_modules/electron/install.js
"$ROOT/root/usr/bin/Xvfb" :99 -screen 0 1280x800x24 >/tmp/plx-xvfb.log 2>&1 &
XVFB=$!
trap 'kill $XVFB 2>/dev/null || true' EXIT
sleep 1
PATH="$HOME/bin:$PATH" DISPLAY=:99 PLX_DESKTOP_SMOKE=1 PLX_ELECTRON="$PWD/node_modules/electron/dist/electron" PW_CORE=${PW_CORE:-$HOME/perf-1122/node_modules/playwright-core} npm test
