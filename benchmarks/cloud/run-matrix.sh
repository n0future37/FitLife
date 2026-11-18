#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$PWD"
export FITLIFE_BENCH_SECRETS="${FITLIFE_BENCH_SECRETS:-$HOME/.config/fitlife-benchmark}"
campaign="artifacts/cloud-benchmark/campaign-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$campaign"
cp benchmarks/cloud/protocol.json "$campaign/protocol.json"
if [ -f source-final-manifest.json ]; then cp source-final-manifest.json "$campaign/source-manifest.json"; fi
compose() {
  sudo env FITLIFE_BENCH_SECRETS="$FITLIFE_BENCH_SECRETS" APP_WORKERS="${APP_WORKERS:-2}" docker compose -f benchmarks/cloud/compose.yaml "$@"
}
ready() {
  for attempt in $(seq 1 60); do
    if compose exec -T app python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/ready', timeout=3)" >/dev/null 2>&1; then return; fi
    sleep 1
  done
  return 1
}
stopfile=""
sampler=""
finish_sampler() {
  if [ -n "$stopfile" ]; then touch "$stopfile"; fi
  if [ -n "$sampler" ]; then wait "$sampler" || true; fi
  stopfile=""
  sampler=""
}
trap finish_sampler EXIT
for workers in 1 2; do
  export APP_WORKERS="$workers"
  compose up -d --force-recreate app
  ready
  stopfile="$root/$campaign/workers-$workers.done"
  sudo python3 benchmarks/cloud/resources.py --output "$root/$campaign/workers-$workers.resources.jsonl" --stop-file "$stopfile" > "$campaign/workers-$workers.sampler.log" 2>&1 &
  sampler=$!
  compose run --rm --no-deps --name "fitlife-benchmark-load-workers-$workers" load python benchmarks/cloud/load.py --workers "$workers" --seconds 20 --repeats 3 --concurrencies 1 8 32 > "$campaign/workers-$workers.log" 2>&1
  finish_sampler
  printf 'Worker configuration %s completed.\n' "$workers"
done
stopfile="$root/$campaign/stress.done"
sudo python3 benchmarks/cloud/resources.py --output "$root/$campaign/stress.resources.jsonl" --stop-file "$stopfile" > "$campaign/stress.sampler.log" 2>&1 &
sampler=$!
compose run --rm --no-deps --name fitlife-benchmark-stress load python benchmarks/cloud/stress.py --resource-file "/app/artifacts/$(basename "$campaign")/stress.resources.jsonl" > "$campaign/stress.log" 2>&1
finish_sampler
compose exec -T app python -m pip freeze > "$campaign/python-packages.txt"
compose ps > "$campaign/container-status.txt"
printf 'Complete campaign: %s\n' "$campaign"
