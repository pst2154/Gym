#!/usr/bin/env bash
#
# LogScale Gym – one-command setup
#
#   1. Start the LogScale container (humio/humio-single-node-demo)
#   2. Wait until it is healthy
#   3. Discover the sandbox repository and its ingest token
#   4. Ingest synthetic_falcon_events.csv
#   5. Verify with a count() query
#
# Usage:
#   bash setup.sh                          # defaults
#   bash setup.sh --url http://host:8080   # custom LogScale URL
#   bash setup.sh --skip-container         # skip docker, just ingest + verify

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGSCALE_URL="${LOGSCALE_URL:-http://localhost:8080}"
CSV_PATH="$SCRIPT_DIR/data/synthetic_falcon_events.csv"
SKIP_CONTAINER=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)           LOGSCALE_URL="$2"; shift 2 ;;
    --skip-container) SKIP_CONTAINER=true; shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

echo "=== LogScale Gym Setup ==="
echo "  URL:  $LOGSCALE_URL"
echo "  CSV:  $CSV_PATH"
echo ""

# ── 1. Start the container ─────────────────────────────────────────
start_container() {
  if docker ps --format '{{.Names}}' | grep -q '^humio$'; then
    echo "[OK] Container 'humio' is already running."
    return
  fi
  if docker ps -a --format '{{.Names}}' | grep -q '^humio$'; then
    echo "[..] Starting stopped container 'humio'..."
    docker start humio
  else
    echo "[..] Creating and starting container 'humio'..."
    docker run -d \
      -p 8080:8080 \
      -p 9200:9200 \
      --name=humio \
      --ulimit="nofile=250000:250000" \
      --stop-timeout 300 \
      -e AUTHENTICATION_METHOD=none \
      -e ELASTIC_PORT=9200 \
      -e "PUBLIC_URL=$LOGSCALE_URL" \
      -e KAFKA_SERVERS=127.0.0.1:9092 \
      -e HUMIO_PORT=8080 \
      humio/humio-single-node-demo
  fi
}

if [ "$SKIP_CONTAINER" = false ]; then
  start_container
fi

# ── 2. Wait for healthy ────────────────────────────────────────────
echo "[..] Waiting for LogScale at $LOGSCALE_URL ..."
retries=90
for i in $(seq 1 $retries); do
  if curl -sf "$LOGSCALE_URL/api/v1/status" > /dev/null 2>&1; then
    echo "[OK] LogScale is healthy (attempt $i)."
    break
  fi
  if [ "$i" -eq "$retries" ]; then
    echo "[FAIL] LogScale did not become healthy after $((retries * 2))s."
    exit 1
  fi
  sleep 2
done

# ── 3. Discover sandbox repo + ingest token ────────────────────────
echo "[..] Discovering sandbox repository..."
REPO=$(python3 -c "
import requests, json
domains = requests.post(
    '$LOGSCALE_URL/graphql',
    json={'query': '{ searchDomains { name } }'},
    headers={'Content-Type': 'application/json'}, timeout=10,
).json()['data']['searchDomains']
sandboxes = [d['name'] for d in domains if d['name'].startswith('sandbox_')]
print(sandboxes[0] if sandboxes else domains[0]['name'])
")
echo "  Repository: $REPO"

echo "[..] Fetching ingest token..."
INGEST_TOKEN=$(python3 -c "
import requests, json
query = '{ searchDomain(name: \"$REPO\") { ... on Repository { ingestTokens { token } } } }'
resp = requests.post(
    '$LOGSCALE_URL/graphql',
    json={'query': query},
    headers={'Content-Type': 'application/json'}, timeout=10,
).json()
print(resp['data']['searchDomain']['ingestTokens'][0]['token'])
")
echo "  Ingest token: ${INGEST_TOKEN:0:12}..."

# ── 4. Ingest CSV ──────────────────────────────────────────────────
if [ ! -f "$CSV_PATH" ]; then
  echo "[FAIL] CSV not found: $CSV_PATH"
  exit 1
fi

echo "[..] Ingesting $CSV_PATH ..."
python3 -c "
import csv, json, requests

with open('$CSV_PATH') as f:
    rows = list(csv.DictReader(f))

print(f'  {len(rows)} events to ingest')
from collections import defaultdict
by_type = defaultdict(list)
for row in rows:
    by_type[row.get('#event_simpleName', 'unknown')].append(row)
print(f'  {len(by_type)} event types')

total = 0
chunk_size = 500
for etype, typed_rows in by_type.items():
    for i in range(0, len(typed_rows), chunk_size):
        chunk = typed_rows[i:i+chunk_size]
        payload = [{
            'tags': {'host': 'sandbox', '#event_simpleName': etype},
            'events': [
                {
                    'timestamp': row.get('timestamp', ''),
                    'attributes': {k: v for k, v in row.items() if v and k not in ('timestamp', '#event_simpleName')},
                }
                for row in chunk
            ],
        }]
        resp = requests.post(
            '$LOGSCALE_URL/api/v1/ingest/humio-structured',
            json=payload,
            headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer $INGEST_TOKEN',
            },
        )
        total += len(chunk)
        if resp.status_code != 200:
            print(f'  ERROR at {total}: {resp.status_code} {resp.text[:120]}')
            exit(1)

print(f'  [OK] {total} events ingested')
"

# ── 5. Verify ───────────────────────────────────────────────────────
echo "[..] Verifying with count() query..."
python3 -c "
import requests, time, json

repo = '$REPO'
url  = '$LOGSCALE_URL'

job = requests.post(
    f'{url}/api/v1/repositories/{repo}/queryjobs',
    json={'queryString': 'count()', 'start': '7days', 'isLive': False},
    headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
).json()
job_id = job.get('id')

for _ in range(30):
    r = requests.get(
        f'{url}/api/v1/repositories/{repo}/queryjobs/{job_id}',
        headers={'Accept': 'application/json'},
    ).json()
    if r.get('done'):
        events = r.get('events', [])
        count = events[0].get('_count', 0) if events else 0
        print(f'  count() = {count}')
        break
    time.sleep(0.5)
"

echo ""
echo "=== Setup complete ==="
echo "  LogScale URL:  $LOGSCALE_URL"
echo "  Repository:    $REPO"
echo ""
echo "Start the Gym resources server:"
echo "  python app.py"
