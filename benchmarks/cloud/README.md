# Dedicated backend benchmark

This is an isolated, fixed-model test deployment. It is **not the production server command**.
Only synthetic data is used. No cloud model key is provided, the model factory is fixed, and
the Compose network has no Internet egress. API port 8100, MySQL and Redis are available only
inside the Docker network, with no published host ports. Do not expose this deployment on the public Internet.

The server now uses `benchmarks.cloud.serve` with `NoDelayH11Protocol`, shared with the normal
server's HTTP transport. It explicitly enables TCP_NODELAY even when inherited worker sockets
report protocol zero under Python 3.12. The client retains idle connections for 2 seconds while
the server uses 5 seconds, leaving a margin at the reuse boundary. No HTTP application retries
are enabled; failed first attempts remain failures. Original experiment sources and results
are immutable; HTTP repair evidence is stored separately in `artifacts/http-repair-20261118`.

## Run on a dedicated Linux host

From the repository root, create a new private credentials directory once:

```sh
python3 benchmarks/cloud/bootstrap.py --directory "$HOME/.config/fitlife-benchmark"
export FITLIFE_BENCH_SECRETS="$HOME/.config/fitlife-benchmark"
mkdir -p artifacts/cloud-benchmark
sudo chown "10001:$(id -g)" artifacts/cloud-benchmark
sudo chmod 2770 artifacts/cloud-benchmark
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml build app
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml up -d --wait mysql redis
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml run --rm --no-deps load python benchmarks/cloud/load.py --migrate
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml up -d app
```

Inside the app container, wait for `http://127.0.0.1:8100/ready` to return 200:

```sh
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml exec -T app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8100/ready', timeout=3).read().decode())"
```

Run each worker configuration separately:

```sh
export APP_WORKERS=1
sudo --preserve-env=FITLIFE_BENCH_SECRETS,APP_WORKERS docker compose -f benchmarks/cloud/compose.yaml up -d --force-recreate app
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml run --rm --no-deps load python benchmarks/cloud/load.py --workers 1
export APP_WORKERS=2
sudo --preserve-env=FITLIFE_BENCH_SECRETS,APP_WORKERS docker compose -f benchmarks/cloud/compose.yaml up -d --force-recreate app
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml run --rm --no-deps load python benchmarks/cloud/load.py --workers 2
```

Do not run two load suites concurrently. The suite requires an empty `fl_users` table and
cleans only the users it creates. Existing business data is never erased automatically.
If interrupted, inspect the failed suite and test database before restarting. Preserve failed
reports rather than deleting results to make a run appear successful.

## Measurement contract

- 32 synthetic users, 100 activity rows per user; seeding is excluded from timing. Activity
  seeds use direct ORM writes with confirmed source actions; they are not HTTP performance samples.
- Separate profile query, 20-row activity page with count/sum, and propose-plus-confirm workflow.
- Closed-loop concurrency 1, 8, 32, twenty seconds each, three repetitions; no think time.
  Drain time is included. These are achieved operation rates, not fixed offered QPS or maximum capacity.
- HTTP/1.1 keep-alive over real TCP on the Docker internal network; authentication, database access
  and transaction time included. One write operation consists of **two HTTP requests**.
- Warm-up samples are stored but excluded from trial statistics. Report successful-operation
  nearest-rank P50/P95/P99 and error counts separately; never merge rejected requests into a
  faster-looking successful latency.
- Concurrent replay checks cover 32 same-key proposals and 32 confirmations, with exactly one
  business row. Also checks no write before confirmation, foreign-owner denial, invalid digest,
  changed payload with same key, invalid input and missing authentication.
- Agent load uses a fixed 2.5-second response followed by a real MCP query and a fixed answer.
  Redis shared leases are observed every 20ms. This verifies admission/replay and read-only side
  effects, not model quality, multi-agent throughput or cloud model latency.
- Results in `artifacts/cloud-benchmark/results/<UTC>-<UUID>` include summaries and individual
  operation records. Tokens, private keys and passwords are omitted. Retain container resource
  samples, image digests, source hashes and host information alongside results.
- App cap: 2 vCPU, 1024MiB; MySQL: 1 vCPU, 1200MiB, 256MiB buffer pool; Redis: 0.25 vCPU,
  128MiB; driver: 1 vCPU, 512MiB. Database pool is 5 connections **per app process**.
  Driver shares the same 4-vCPU host. CPU competition, driver saturation and short-run effects
  limit extrapolation; no production QPS guarantee or public-network P95 follows from this test.

The existing in-process `fitlife.performance_check` and all prior experiment results remain separate.
Stop this test stack with `docker compose -f benchmarks/cloud/compose.yaml stop` (same secret variable).
Stopping preserves MySQL volume and all reports; this document does not prescribe volume deletion.

`bash benchmarks/cloud/run-matrix.sh` runs the frozen 54-trial matrix and then a bounded
read-only overload ramp on two workers. The ramp tests concurrency 64, 128, 256 for up to
two 30-second trials each. It stops on three readiness failures, less than 300MiB host
available RAM, at least 5% operation errors, successful P95 above 5 seconds, or the load
generator averaging at least 90% of its one-vCPU limit. These are engineering stop conditions,
not a product SLO. In-flight requests drain before a 15-second single-concurrency recovery test.
Short smoke tests remain separate from the formal matrix; all failed reports are retained.

After the matrix, run the isolated Redis timeout/recovery mechanism check:

```sh
sudo --preserve-env=FITLIFE_BENCH_SECRETS docker compose -f benchmarks/cloud/compose.yaml run --rm --no-deps load python benchmarks/cloud/fault_check.py
```

It pauses only this dedicated Redis instance for seven seconds, verifies database-only reads
remain available while Agent admission fails closed, retries the original request key after
recovery and checks cached replay. It is one deterministic mechanism case, not an availability rate.

The first cloud campaign is preserved with its original sampler and source archive. Its sampler
hit a load-container removal race at teardown; current `resources.py` skips a vanished container
sample, records a lifecycle event and continues. Unrelated Docker failures still raise an error.
No historical samples or performance results are filled in or replaced by this utility fix.
