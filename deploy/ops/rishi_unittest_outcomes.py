"""Run unittest modules and write one 'test_id outcome' line per test to OUT.

Usage (from inside a tree): python3 rishi_unittest_outcomes.py OUT [--jobs N] [--timeout S] module ...
Each module runs in its own interpreter with a timeout (a hung module is one
'<module> TIMEOUT' row, never a pass); an import failure is one '<module> IMPORT_ERROR' row.
"""
import os, signal, subprocess, sys, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor


def run_one(out, mod):
    sys.path.insert(0, os.getcwd())
    rows = []

    class R(unittest.TextTestResult):
        def addSuccess(s, t): super().addSuccess(t); rows.append((t.id(), "ok"))
        def addFailure(s, t, e): super().addFailure(t, e); rows.append((t.id(), "FAIL"))
        def addError(s, t, e): super().addError(t, e); rows.append((getattr(t, "id", lambda: str(t))(), "ERROR"))
        def addSkip(s, t, r): super().addSkip(t, r); rows.append((t.id(), "skipped"))
        def addExpectedFailure(s, t, e): super().addExpectedFailure(t, e); rows.append((t.id(), "xfail"))
        def addUnexpectedSuccess(s, t): super().addUnexpectedSuccess(t); rows.append((t.id(), "xpass"))

    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(mod)
    except BaseException as e:  # noqa: BLE001 - any import-time failure is one row
        rows.append((mod, "IMPORT_ERROR"))
        print(f"{mod}: {type(e).__name__}: {e}", file=sys.stderr)
        suite = None
    if suite is not None:
        unittest.TextTestRunner(resultclass=R, verbosity=0, stream=sys.stderr).run(suite)
    with open(out, "w") as fh:
        for i, o in rows:
            fh.write(f"{i} {o}\n")


def main():
    args = sys.argv[1:]
    if args[0] == "--one":
        return run_one(args[1], args[2])
    out, args = args[0], args[1:]
    jobs, timeout = 4, 900
    while args and args[0].startswith("--"):
        if args[0] == "--jobs":
            jobs = int(args[1])
        elif args[0] == "--timeout":
            timeout = int(args[1])
        args = args[2:]
    mods = list(dict.fromkeys(args))
    tmp = tempfile.mkdtemp(prefix="rishi_outcomes.")

    def one(mod):
        part = os.path.join(tmp, mod)
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--one", part, mod],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            _, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)   # the module and any child it started
            p.communicate()
            return [f"{mod} TIMEOUT"], f"{mod}: TIMEOUT after {timeout}s"
        err = err.decode(errors="replace")
        if not os.path.exists(part):
            return [f"{mod} CRASH"], err[-2000:]
        return open(part).read().split("\n")[:-1], err[-4000:]

    rows = []
    with ThreadPoolExecutor(jobs) as ex:
        for mod, (r, err) in zip(mods, ex.map(one, mods)):
            rows += r
            sys.stderr.write(f"=== {mod}\n{err}\n")
    with open(out, "w") as fh:
        fh.write("\n".join(sorted(rows)) + "\n")


if __name__ == "__main__":
    main()
