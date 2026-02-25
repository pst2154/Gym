## LogScale CQL Gym Resources Server

A standalone NeMo Gym environment that evaluates natural-language-to-CQL
queries against a live LogScale container.

---

### 1. Pull and Start the LogScale Container

LogScale (formerly Humio) runs as a single Docker container for development.

```bash
docker pull humio/humio-single-node-demo
```

Start it with port 8080 exposed and authentication disabled:

```bash
docker run -d \
  -p 8080:8080 \
  --name=humio \
  --ulimit="nofile=250000:250000" \
  -e AUTHENTICATION_METHOD=none \
  humio/humio-single-node-demo
```

The container takes 30-60 seconds to start. Check if it's ready:

```bash
curl http://localhost:8080/api/v1/status
```

When you see `"status":"ok"`, LogScale is running. You can also open
http://localhost:8080 in a browser.

To stop or restart later:

```bash
docker stop humio      # stop
docker start humio     # restart (data persists)
docker rm -f humio     # remove entirely
```

### 2. Set Up Your API Key

The LLM judge (for semantic and execution scoring) calls an OpenAI-compatible
API. You need an API key.

Create or edit `~/.env` and add your key:

```
SONNET_46_KEY=your-api-key-here
```

The server reads this file on startup. The default model is
`aws/anthropic/bedrock-claude-opus-4-6` via `https://inference-api.nvidia.com`.
You can override these with environment variables:

| Variable | Default | What it does |
|---|---|---|
| `SONNET_46_KEY` | (required) | API key for the LLM judge |
| `JUDGE_API_URL` | `https://inference-api.nvidia.com` | Base URL of the API |
| `JUDGE_MODEL` | `aws/anthropic/bedrock-claude-opus-4-6` | Model to use |

Without an API key, the Gym still works but only scores validity (0/1)
and gives a flat execution score (5 if the query ran, 1 if it errored).
Semantic score will be `null`.

### 3. Find Your Repository and Ingest Token

LogScale organizes data into repositories. The demo container creates a
sandbox repo automatically. You need to find its name and ingest token.

**Find the repository name** (via the GraphQL API):

```bash
curl -s http://localhost:8080/graphql \
  -H 'content-type: application/json' \
  -d '{"query": "{ searchDomains { name } }"}' | python3 -m json.tool
```

You'll see something like:

```json
{"data": {"searchDomains": [{"name": "sandbox_zWlT5iVVpAnhkcUNRgSMb13J"}]}}
```

Copy that repository name. Now **get the ingest token**:

```bash
curl -s http://localhost:8080/graphql \
  -H 'content-type: application/json' \
  -d '{"query": "{ searchDomain(name: \"sandbox_zWlT5iVVpAnhkcUNRgSMb13J\") { ... on Repository { ingestTokens { token name } } } }"}' \
  | python3 -m json.tool
```

Copy the `token` value. You'll need it for ingestion.

### 4. Ingest Data

The file `data/synthetic_falcon_events.csv` contains 5000 synthetic
CrowdStrike Falcon endpoint events (process executions, network connections,
DNS requests, logon events, etc.).

To ingest it, you send batches of events to the LogScale structured
ingest API using your ingest token:

```bash
REPO="sandbox_zWlT5iVVpAnhkcUNRgSMb13J"   # your repo name from step 3
TOKEN="your-ingest-token-from-step-3"

python3 -c "
import csv, json, requests

with open('data/synthetic_falcon_events.csv') as f:
    rows = list(csv.DictReader(f))

print(f'{len(rows)} events to ingest')

for i in range(0, len(rows), 500):
    chunk = rows[i:i+500]
    payload = [{
        'tags': {'host': 'sandbox'},
        'events': [
            {'timestamp': row.get('timestamp', ''),
             'attributes': {k: v for k, v in row.items() if v and k != 'timestamp'}}
            for row in chunk
        ],
    }]
    resp = requests.post(
        'http://localhost:8080/api/v1/ingest/humio-structured',
        json=payload,
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer $TOKEN',
        },
    )
    print(f'  Ingested {min(i+500, len(rows))}/{len(rows)}  (HTTP {resp.status_code})')
"
```

Verify the data is there:

```bash
curl -s http://localhost:8080/api/v1/repositories/$REPO/queryjobs \
  -H 'content-type: application/json' \
  -d '{"queryString":"count()","start":"7days","isLive":false}' \
  | python3 -m json.tool
```

Or just run `setup.sh` which does steps 1-4 automatically:

```bash
bash setup.sh
```

### 5. Install the Gym

From the Gym repo root:

```bash
cd Gym
uv venv && uv pip install -e .
```

This installs `nemo_gym` and the `ng_run` CLI.

### 6. Start the Gym Server

```bash
cd Gym
source .venv/bin/activate
ng_run "+config_paths=[resources_servers/logscale_cql/configs/logscale_cql.yaml]"
```

Wait ~30 seconds until you see `All 1 / 1 servers ready`.

The Gym runs two things:
- **Head server** on port 11000 — manages server instances
- **logscale_cql resources server** on a random port — does the actual work

Find the resources server URL:

```bash
curl -s http://127.0.0.1:11000/server_instances | python3 -m json.tool
```

### 7. Generate Evaluation Questions

The Gym can generate evaluation questions by querying LogScale for the
available fields and event types, then asking the LLM to create questions:

```bash
curl -s http://127.0.0.1:<PORT>/generate_questions \
  -H 'content-type: application/json' \
  -d '{"count": 10, "difficulty": 2}'
```

`difficulty` ranges from 1 (easy) to 5 (adversarial). The response includes
the generated questions plus the discovered fields and event types.

### 8. Score a Query

Send a question and a model's CQL response to `/verify`:

```bash
curl -s http://127.0.0.1:<PORT>/verify \
  -H 'content-type: application/json' \
  -d '{
    "question": "How many events total?",
    "responses_create_params": {"input": ""},
    "response": {
      "id": "r0", "created_at": 0, "model": "my-model", "object": "response",
      "parallel_tool_calls": false, "tool_choice": "none", "tools": [],
      "output": [{"id": "m0", "type": "message", "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "count()", "annotations": []}]
      }]
    }
  }'
```

The Gym will:
1. Extract the CQL from the model output (`count()`)
2. Validate syntax against LogScale (`validity_score`: 0 or 1)
3. Execute the query and get results
4. Ask the LLM judge to score semantic match and execution correctness
5. Return a combined reward (0.0 to 1.0)

### How Scoring Works

| Component | Source | Range | What it measures |
|---|---|---|---|
| Validity | LogScale `validateQuery` GraphQL | 0 or 1 | Is the CQL syntactically valid? |
| Semantic | LLM judge | 1-5 | Does the query match what the user asked? |
| Execution | LLM judge | 1-5 | Do the results look correct for the question? |

**Reward** = `(validity + semantic/5 + execution/5) / 3`

A perfect query gets `(1 + 1.0 + 1.0) / 3 = 1.0`.
An invalid query gets `(0 + 0 + 0) / 3 = 0.0`.

### Configuration

`configs/logscale_cql.yaml`:

```yaml
logscale_cql:
  resources_servers:
    logscale_cql:
      entrypoint: app.py
      domain: knowledge
      engine: container
      logscale_url: http://localhost:8080
```

| Option | Default | Description |
|---|---|---|
| `engine` | `container` | `container` = live LogScale; `local` = pandas fallback |
| `logscale_url` | `http://localhost:8080` | Where LogScale is running |
| `repository` | auto-detected | Set manually if you have multiple repos |

### Files

| File | What it is |
|---|---|
| `setup.sh` | Automates steps 1-4 (container + ingest + verify) |
| `app.py` | The Gym resources server (FastAPI) |
| `container_engine.py` | Talks to LogScale (query execution + validation) |
| `configs/logscale_cql.yaml` | Server configuration |
| `data/synthetic_falcon_events.csv` | 5000 synthetic CrowdStrike Falcon events |
