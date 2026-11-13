"""The host's job queue: every heavy job (the delta cycle, the proactive passes, the search
follower, landings, test runs) waits in one Postgres queue and starts only when the
admission check says the machine has the memory for it.

- ``classes``: what each job class declares (memory, CPUs, priority, concurrency, retries).
- ``queue``: the schema, enqueue, admission and claims (``FOR UPDATE SKIP LOCKED``), leases,
  retries with backoff, the dead letter, preemption marks and the status read.
- ``worker``: the daemon that starts unit jobs (their own systemd services), follows them,
  reaps expired leases and preempts under pressure.
- ``inline``: a job its own caller runs (``tools/land.sh``, ``tools/testslot.py``,
  ``python3 -m operating.jobq run``).
- ``hygiene``: every process a job spawned is ended with the job.
- ``status``: the snapshot for ``/api/ops/status`` and the CLI.

stdlib plus psycopg only."""
