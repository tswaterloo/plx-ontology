"""``python3 -m operating.spark_worker health|test|embed``: the standing worker that sends test
suites and embedding passes to the owner's DGX Sparks over the tailnet, with a health
check and a local fallback (AUDIT3-24).

* **health** (``plx-spark-health.timer``, every 10 minutes): one ``ssh`` call per host
  (``PLX_SPARK_HOSTS``, default ``s7d3d,sb4ec``; ``PLX_SPARK_SSH_CONFIG``, the tailnet proxy and
  known hosts) reads its CPUs, load, available memory, free disk, whether the test
  sandbox (``~/plx-tests``: Postgres and the venv) is installed, and its GPU. A host is
  fit for **tests** when it answers, its load is under ``MAX_LOAD`` per CPU, it has
  ``MIN_MEM_GB`` available and ``MIN_DISK_GB`` free and the sandbox is there; for
  **embed** when it also has a GPU and is near idle (``MAX_EMBED_LOAD``). Written to ``<state>/spark/health.json`` (counts,
  never an address). A probe that cannot be read is unfit (fail closed).
* **test** ``<sha> [-t module|@file]...``: the fit hosts run the commit's modules
  (``deploy/spark/spark_test.sh``: only the commit's git objects go over, run against a
  throwaway Postgres on the host). No fit host, or a setup failure there (exit 96-99):
  the same modules run here, in a detached checkout of the commit under
  ``tools/testslot.py`` (the machine's cap on test runs). A module failing on the Spark is
  a real result, never retried locally.
* **embed** ``--state D``: the pending search backlog on the fit GPU hosts
  (``operating.embed remote``, its parity check and resumable cursor). No fit host, or
  the remote run failing: the CPU backfill here (``operating.embed backfill``), at low
  priority.
* Evals are not sent: they read the served data, which stays on this box.

Read-only towards business systems; nothing here holds or passes a credential (every
child runs with the credential-shaped variables removed). Exit codes: the child's (0 done,
1 failed), 2 refused, 96 no host and the fallback refused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping, Optional, Sequence

from ontology import tmp_root

HOSTS_ENV = "PLX_SPARK_HOSTS"
SSH_CONFIG_ENV = "PLX_SPARK_SSH_CONFIG"
DEFAULT_HOSTS = ("s7d3d", "sb4ec")   # both Sparks (AUDIT3-24: one host left the other unwatched)
DEFAULT_SSH_CONFIG = "/data/plx-state/ts-petra/ssh_config"
HEALTH_FILE = Path("spark") / "health.json"
SCHEMA = "plx-spark-health/v1"
# load per CPU above which a host is busy: a suite shares a host already running one (its
# 16 workers on 20 cores read ~3.8 per CPU, measured 11-14) rather than load this box;
# an embedding pass wants the host near idle
MAX_LOAD = 6.0
MAX_EMBED_LOAD = 1.5
MIN_MEM_GB = 8.0
MIN_DISK_GB = 20.0
PROBE_SECONDS = 20
MAX_AGE = dt.timedelta(minutes=30)   # an older health file is read as no file
SETUP_FAILED = range(96, 100)        # spark_test.sh: the host could not run the suite
REPO = Path(__file__).resolve().parents[1]
_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
# names of variables never handed to a child (the API key, app secrets, tokens)
_SECRET = re.compile(r"(SECRET|TOKEN|PASSWORD|API_KEY|_KEY$|CREDENTIAL)", re.I)

# One line of JSON from the host: numbers and a flag only. nvidia-smi may be missing (0
# GPUs); the GB10's unified memory reports no GPU memory total, so GPUs are counted.
PROBE = (
    "printf '{\"cpus\":%s,\"load1\":%s,\"mem_avail_kb\":%s,\"disk_free_kb\":%s,"
    "\"tests_ready\":%s,\"gpus\":%s}\\n' "
    "\"$(nproc)\" \"$(cut -d' ' -f1 /proc/loadavg)\" "
    "\"$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)\" "
    "\"$(df -Pk \"$HOME\" | awk 'NR==2{print $4}')\" "
    "\"$([ -x \"$HOME/plx-tests/pg/bin/pg_ctl\" ] && [ -d \"$HOME/plx-tests/venv\" ] "
    "&& echo true || echo false)\" "
    "\"$(nvidia-smi -L 2>/dev/null | grep -c '^GPU')\""
)

Runner = Callable[..., subprocess.CompletedProcess]


def hosts_named(value: Optional[str] = None) -> tuple[str, ...]:
    """The configured hosts (``PLX_SPARK_HOSTS``, comma separated); a name that is not a
    plain host alias is refused (it reaches an ssh command line)."""
    raw = value if value is not None else os.environ.get(HOSTS_ENV, "")
    names = tuple(h.strip() for h in raw.split(",") if h.strip()) or DEFAULT_HOSTS
    bad = [h for h in names if not _HOST.match(h)]
    if bad:
        raise ValueError("spark_host_invalid")
    return names


def clean_env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """This process's environment without a credential-shaped variable."""
    env = {k: v for k, v in os.environ.items() if not _SECRET.search(k)}
    env.update(extra or {})
    return env


def _ssh(config: str) -> list[str]:
    return ["ssh", "-F", config, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def read_probe(text: str) -> Optional[dict[str, Any]]:
    """The probe's line, its fields typed; None for anything else (fail closed)."""
    try:
        raw = json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if not isinstance(raw, dict) or raw.get("tests_ready") not in (True, False):
        return None
    out: dict[str, Any] = {"tests_ready": raw["tests_ready"]}
    for key in ("cpus", "load1", "mem_avail_kb", "disk_free_kb", "gpus"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
        out[key] = value
    return out if out["cpus"] >= 1 else None


def judge(probe: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """One host's fitness for tests and for embedding, with the reasons it is not."""
    if probe is None:
        return {"reachable": False, "tests": False, "embed": False,
                "problems": ["unreachable_or_unreadable"]}
    problems = []
    per_cpu = probe["load1"] / probe["cpus"]
    if per_cpu > MAX_LOAD:
        problems.append("busy")
    if probe["mem_avail_kb"] / 1048576 < MIN_MEM_GB:
        problems.append("memory_low")
    if probe["disk_free_kb"] / 1048576 < MIN_DISK_GB:
        problems.append("disk_low")
    fit = not problems
    tests = fit and probe["tests_ready"]
    embed = fit and probe["gpus"] >= 1 and per_cpu <= MAX_EMBED_LOAD
    if fit and per_cpu > MAX_EMBED_LOAD:
        problems.append("busy_for_embedding")
    if fit and not probe["tests_ready"]:
        problems.append("test_sandbox_missing")
    if fit and probe["gpus"] < 1:
        problems.append("no_gpu")
    return {"reachable": True, "tests": tests, "embed": embed, "problems": problems,
            "cpus": int(probe["cpus"]), "load_per_cpu": round(per_cpu, 2),
            "mem_avail_gb": round(probe["mem_avail_kb"] / 1048576, 1),
            "disk_free_gb": round(probe["disk_free_kb"] / 1048576, 1),
            "gpus": int(probe["gpus"])}


def health(hosts: Sequence[str], config: str, run: Runner = subprocess.run
           ) -> dict[str, Any]:
    """Every host probed (one ssh call each, ``PROBE_SECONDS`` at most)."""
    out = {}
    for host in hosts:
        try:
            done = run([*_ssh(config), host, PROBE], capture_output=True, text=True,
                       timeout=PROBE_SECONDS, env=clean_env())
            probe = read_probe(done.stdout) if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            probe = None
        out[host] = judge(probe)
    return {"schema": SCHEMA,
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "hosts": out}


def write_health(state: Path, found: Mapping[str, Any]) -> Path:
    path = state / HEALTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def fit_hosts(found: Mapping[str, Any], purpose: str) -> list[str]:
    return [h for h, j in sorted((found.get("hosts") or {}).items())
            if isinstance(j, Mapping) and j.get(purpose) is True]


def _modules(items: Sequence[str]) -> list[str]:
    """``-t`` values: module names or ``@file`` lists (one per line, ``#`` comments)."""
    out: list[str] = []
    for item in items:
        names = [item]
        if item.startswith("@"):
            names = [ln.split("#", 1)[0].strip() for ln in
                     Path(item[1:]).read_text(encoding="utf-8").splitlines()]
        for name in names:
            if not name:
                continue
            if not _MODULE.match(name):
                raise ValueError("module_name_invalid")
            out.append(name)
    return list(dict.fromkeys(out))


def run_tests(sha: str, modules: Sequence[str], *, hosts: Sequence[str], config: str,
              run: Runner = subprocess.run, repo: Path = REPO, log=print) -> int:
    """The commit's modules on the fit hosts, else here (module doc, **test**)."""
    if not _SHA.match(sha):
        raise ValueError("sha_invalid")
    if hosts:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(modules) + "\n")
            listed = f.name
        try:
            env = clean_env({"SPARK_HOST": hosts[0] if len(hosts) == 1 else "both",
                             SSH_CONFIG_ENV: config, "LAND_REPO": str(repo)})
            done = run(["bash", str(repo / "deploy" / "spark" / "spark_test.sh"), sha,
                        "-t", "@" + listed], env=env)
        finally:
            os.unlink(listed)
        if done.returncode not in SETUP_FAILED:
            return done.returncode
        log(json.dumps({"spark": "setup_failed", "exit": done.returncode,
                        "fallback": "local"}))
    else:
        log(json.dumps({"spark": "no_fit_host", "fallback": "local"}))
    return run_local_tests(sha, modules, run=run, repo=repo)


def run_local_tests(sha: str, modules: Sequence[str], *, run: Runner = subprocess.run,
                    repo: Path = REPO) -> int:
    """The modules here, from a detached checkout of ``sha`` (never the live tree)."""
    where = Path(tmp_root.mkdtemp(prefix="spark-fallback-"))
    tree = where / "tree"
    try:
        made = run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree),
                    sha], env=clean_env())
        if made.returncode != 0:
            return 96
        done = run([sys.executable, "tools/testslot.py", sys.executable, "-m", "unittest",
                    *modules], cwd=str(tree), env=clean_env())
        return done.returncode
    finally:
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)],
            env=clean_env())
        shutil.rmtree(where, ignore_errors=True)


def run_embed(state: str, *, hosts: Sequence[str], config: str, extra: Sequence[str] = (),
              run: Runner = subprocess.run, log=print) -> int:
    """The pending backlog on the fit GPU hosts, else the CPU backfill here."""
    if hosts:
        args = [sys.executable, "-m", "operating.embed", "remote", "--state", state,
                "--ssh-config", config, *(a for h in hosts for a in ("--host", h)), *extra]
        done = run(args, env=clean_env())
        if done.returncode in (0, 2):   # done, or refused (a refusal is not retried here)
            return done.returncode
        log(json.dumps({"spark": "embed_failed", "exit": done.returncode,
                        "fallback": "local"}))
    else:
        log(json.dumps({"spark": "no_gpu_host", "fallback": "local"}))
    return run(["nice", "-n", "15", sys.executable, "-m", "operating.embed", "backfill",
                "--state", state], env=clean_env()).returncode


def load_health(state: Path, now: Optional[dt.datetime] = None) -> Optional[dict[str, Any]]:
    """The timer's last health file when fresh (``MAX_AGE``), else None."""
    try:
        found = json.loads((state / HEALTH_FILE).read_text(encoding="utf-8"))
        at = dt.datetime.fromisoformat(found["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if found.get("schema") != SCHEMA:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return found if now - at <= MAX_AGE else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="operating.spark_worker")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("health")
    h.add_argument("--state", help="write <state>/spark/health.json")
    t = sub.add_parser("test")
    t.add_argument("sha")
    t.add_argument("-t", dest="modules", action="append", default=[])
    e = sub.add_parser("embed")
    e.add_argument("--state", required=True)
    e.add_argument("--limit", type=int)
    for q in (h, t, e):
        q.add_argument("--hosts", help=f"comma separated (default ${HOSTS_ENV} or s7d3d,sb4ec)")
    args = p.parse_args(argv)
    try:
        hosts = hosts_named(args.hosts)
    except ValueError as refused:
        print(json.dumps({"refused": str(refused)}))
        return 2
    config = os.environ.get(SSH_CONFIG_ENV) or DEFAULT_SSH_CONFIG
    found = health(hosts, config)
    if args.cmd == "health":
        if args.state:
            write_health(Path(args.state), found)
        print(json.dumps(found, sort_keys=True))
        return 0
    if args.cmd == "test":
        try:
            modules = _modules(args.modules)
            if not modules:
                raise ValueError("modules_required")
            return run_tests(args.sha, modules, hosts=fit_hosts(found, "tests"),
                             config=config)
        except (ValueError, OSError) as refused:
            print(json.dumps({"refused": str(refused)[:80]}))
            return 2
    extra = ["--limit", str(args.limit)] if args.limit else []
    return run_embed(args.state, hosts=fit_hosts(found, "embed"), config=config, extra=extra)


if __name__ == "__main__":
    sys.exit(main())
