"""Timers that must keep running: the shipped unit plus its host drop-in, read the way
systemd does (an empty assignment resets the list), schedule on the clock. A timer that
re-arms from its service's end (OnUnitInactiveSec) goes to "next run: infinity" after a
trigger the service did not take (seen on plx-proactive-inc, 11-13) and stays there.
Evidence label: ``isolated-fixture``."""
from __future__ import annotations

from pathlib import Path
import re
import unittest

SYSTEMD = Path(__file__).resolve().parent.parent / "deploy" / "systemd"
CLOCK_TIMERS = ("plx-proactive-inc", "plx-truth-check", "plx-ontology-pg-follow")


def effective(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    files = [SYSTEMD / f"{path.name}.timer"]
    drop = SYSTEMD / "host" / f"{path.name}.timer.d"
    files += sorted(drop.glob("*.conf")) if drop.is_dir() else []
    for f in files:
        for raw in f.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "[")) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out.setdefault(key.strip(), [])
            if value.strip() == "":
                out[key.strip()] = []
            else:
                out[key.strip()].append(value.strip())
    return out


class TimerUnitTests(unittest.TestCase):
    def test_refresh_timers_run_on_the_clock(self):
        for name in CLOCK_TIMERS:
            with self.subTest(timer=name):
                timer = effective(SYSTEMD / name)
                self.assertTrue(timer.get("OnCalendar"), f"{name}: no OnCalendar")
                self.assertFalse(timer.get("OnUnitInactiveSec"), f"{name}: re-arms from its service")
                self.assertFalse(timer.get("OnUnitActiveSec"), f"{name}: re-arms from its service")


class FallbackAndDebounceTests(unittest.TestCase):
    def test_a_timer_that_re_arms_from_its_service_also_runs_on_the_clock(self):
        """11-13: plx-autopilot-follow.timer showed no next run after an ExecCondition
        skip; a fixed-schedule fallback keeps it running."""
        timer = effective(SYSTEMD / "plx-autopilot-follow")
        self.assertTrue(timer.get("OnCalendar"))

    def test_the_truth_check_debounce_reaches_the_shell_unexpanded(self):
        """REL-R-04: systemd expands %s, %Y and $f before sh sees them; the debounce must
        write %% and $$ so the shell gets date +%s, stat -c %Y and $f."""
        unit = (SYSTEMD / "plx-truth-check.service").read_text()
        cond = [l for l in unit.splitlines() if l.startswith("ExecCondition=")]
        self.assertEqual(len(cond), 1, cond)
        line = cond[0]
        import re
        self.assertEqual(re.findall(r"(?<![%])%[sY]", line.replace("%%", "")), [])
        self.assertEqual(re.findall(r"(?<!\$)\$(?=[a-z(])", line.replace("$$", "")), [])
        self.assertIn("date +%%s", line)
        self.assertIn("stat -c %%Y $$f", line)


class CaddyFeedTests(unittest.TestCase):
    def test_the_desktop_update_feed_is_served_ahead_of_the_web_app(self):
        text = (SYSTEMD.parent / "Caddyfile").read_text()
        feed, app = text.find("handle_path /desktop/*"), text.find("# Everything else is the web app")
        self.assertTrue(0 < feed < app, "no /desktop/ route ahead of the catch-all")
        block = text[feed:app]
        self.assertIn("file_server", block)
        self.assertNotIn("reverse_proxy", block)

    def test_the_feed_page_may_style_itself_and_nothing_else(self):
        # the live walk found /desktop/'s download page unstyled: its inline <style> was refused
        text = (SYSTEMD.parent / "Caddyfile").read_text()
        block = text[text.find("handle_path /desktop/*"):text.find("# Everything else is the web app")]
        csp = re.search(r'Content-Security-Policy "([^"]+)"', block).group(1)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("style-src 'unsafe-inline'", csp)
        self.assertNotIn("script-src", csp)
        self.assertIn("frame-ancestors 'none'", csp)


if __name__ == "__main__":
    unittest.main()


class DeltaConsumerOrderingTests(unittest.TestCase):
    """11-15: plx-autopilot-follow's start job waited behind a delta cycle running back to
    back (After=plx-delta.service), so the live feed ran no cycle for hours. The delta's
    readers take in only the layers Postgres applied, each in one transaction, so they need
    no ordering after the delta and must not wait on it."""
    READERS = ("plx-autopilot-follow", "plx-autopilot-fast", "plx-proactive-inc",
               "plx-ai-watcher", "plx-truth-check")
    LONG = ("plx-delta.service", "plx-delta-compact.service")

    def test_delta_readers_do_not_wait_for_a_delta_cycle(self):
        for name in self.READERS:
            with self.subTest(unit=name):
                files = [SYSTEMD / f"{name}.service"]
                drop = SYSTEMD / "host" / f"{name}.service.d"
                files += sorted(drop.glob("*.conf")) if drop.is_dir() else []
                after = []
                for f in files:
                    for raw in f.read_text().splitlines():
                        key, _, value = raw.strip().partition("=")
                        if key.strip() in ("After", "Requires", "BindsTo", "Requisite"):
                            after += value.split()
                self.assertFalse(set(after) & set(self.LONG), f"{name}: {after}")
