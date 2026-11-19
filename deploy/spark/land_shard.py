"""Land module list: smoke (unless NOSMOKE=1) + extra (ALL: every test module) minus known red; modules slower than SHARD_S (from timings)
split into their TestCase classes (exactly what unittest's module loader would run), so one slow
module no longer sets the pace. Run with cwd = the merged tree. Prints one id per line."""
import json, os, pathlib, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from tools.main_ci import known_red
SHARD_S = float(os.environ.get("LAND_SHARD_SECONDS", "90"))
timings = {}
try: timings = json.loads(pathlib.Path(os.environ.get("LAND_TIMINGS", "/data/scratch/land-timings.json")).read_text())
except Exception: pass
kr = pathlib.Path("tools/KNOWN-RED.md")
red = known_red(kr.read_text()) if kr.is_file() else set()
# the smoke set is the lander's (every landing runs it)
smoke = [] if os.environ.get("NOSMOKE") == "1" else \
    [l.partition("#")[0].strip() for l in pathlib.Path("tools/smoke-tests.txt").read_text().splitlines()]
args = sys.argv[1:]
if "ALL" in args:   # tools/affected_tests.py could not bound the change: every test module
    from tools.main_ci import discover
    args = [m for m in args if m != "ALL"] + discover(".")
want = {m for m in [*smoke, *args] if m}
missing = sorted(m for m in want if not pathlib.Path(m.split(".")[0] if False else m.replace(".", "/") + ".py").is_file()
                 and not pathlib.Path(".".join(m.split(".")[:-1]).replace(".", "/") + ".py").is_file())
if missing: print(f"skipped modules that do not exist in the tree: {missing}", file=sys.stderr)
mods = sorted(want - red - set(missing))
print(f"skipped known red: {sorted(want & red)}", file=sys.stderr)
ENUM = r'''
import importlib, sys, unittest
m = importlib.import_module(sys.argv[1])
if hasattr(m, "load_tests"): sys.exit(3)
L = unittest.defaultTestLoader
for n in sorted(dir(m)):
    o = getattr(m, n)
    if isinstance(o, type) and issubclass(o, unittest.TestCase) and L.loadTestsFromTestCase(o).countTestCases():
        print(n)
'''
def split(mod):
    if float(timings.get(mod, 0)) < SHARD_S: return [mod]
    try:
        p = subprocess.run([sys.executable, "-c", ENUM, mod], text=True, capture_output=True, timeout=90,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        names = p.stdout.split()
        if p.returncode != 0 or len(names) < 2: return [mod]
        return [f"{mod}.{n}" for n in names]
    except Exception:
        return [mod]
with ThreadPoolExecutor(16) as ex:
    out = [i for ids in ex.map(split, mods) for i in ids]
print(f"sharded: {len(mods)} modules -> {len(out)} jobs", file=sys.stderr)
print("\n".join(out))
