## LogScale CQL Gym Resources Server

A standalone NeMo Gym resources server that evaluates natural-language-to-CQL
translation by executing generated CQL queries against a live
[Falcon LogScale](https://library.humio.com/deployment/installation-containers-humio.html)
container.

### Prerequisites

- Docker
- Python >= 3.9
- `pip install requests pandas`

### LLM Judge Configuration

The semantic and execution scores use an LLM judge via an OpenAI-compatible API.
Set these environment variables (or add them to `~/.env`):

| Variable | Default | Description |
|---|---|---|
| `SONNET_46_KEY` | — | API key for the judge LLM (required for full scoring) |
| `NVIDIA_API_URL` | `https://inference-api.nvidia.com` | Base URL of the OpenAI-compatible API |
| `LLM_MODEL` | `aws/anthropic/bedrock-claude-opus-4-6` | Model name for the judge |

```bash
# Option A: export directly
export SONNET_46_KEY="your-api-key-here"
export NVIDIA_API_URL="https://inference-api.nvidia.com"
export LLM_MODEL="aws/anthropic/bedrock-claude-opus-4-6"

# Option B: add to ~/.env (auto-loaded)
echo 'SONNET_46_KEY=your-api-key-here' >> ~/.env
```

Without an API key, `test_standalone.py` falls back to validity + binary
execution scoring only (no semantic score).

### Quick Start

All commands below are run from `Gym/resources_servers/logscale_cql/`.

```bash
cd Gym/resources_servers/logscale_cql

# 1. Start LogScale and ingest synthetic data (takes ~2 min on first run)
bash setup.sh

# 2. Test the container engine directly
python -c "
from container_engine import LogScaleContainerEngine
engine = LogScaleContainerEngine()
print('Repo:', engine.repository)
print(engine.execute('count()'))
print(engine.validate_query('ProcessRollup2 | count()'))
"

# 3. Run the standalone scoring test with LLM judge
python test_standalone.py

# 3b. Or without the LLM judge (no API key needed)
python test_standalone.py --no-judge

# 4. Start the Gym resources server (requires nemo_gym + ray)
cd ../../
PYTHONPATH=. python resources_servers/logscale_cql/app.py
```

### Full End-to-End Example (Gym `ng_run` + `curl`)

This is the “real” Gym workflow: start the LogScale container, start NeMo Gym
(`ng_run`), discover the resources-server URL from the head server, then send a
query for scoring/execution.

From the repo root:

```bash
# 1) Start LogScale + ingest the synthetic dataset
cd Gym/resources_servers/logscale_cql
bash setup.sh

# 2) Start NeMo Gym (head server + logscale_cql resources server)
cd ../../
source .venv/bin/activate
ng_run "+config_paths=[resources_servers/logscale_cql/configs/logscale_cql.yaml]"
```

In another terminal, discover the `logscale_cql` URL (its port is dynamically assigned):

```bash
curl -s http://127.0.0.1:11000/server_instances | python -m json.tool
```

You should see something like:

```json
[
  {
    "name": "logscale_cql",
    "url": "http://127.0.0.1:12948",
    "...": "..."
  }
]
```

Now send a query for scoring/execution (replace `12948` with whatever your head server reported):

```bash
curl -s http://127.0.0.1:12948/verify \
  -H 'content-type: application/json' \
  -d '{
    "question":"How many events are in the dataset?",
    "responses_create_params":{"input":""},
    "response":{
      "id":"resp_0",
      "created_at":0,
      "model":"gpt-4o-mini",
      "object":"response",
      "parallel_tool_calls":false,
      "tool_choice":"none",
      "tools":[],
      "output":[
        {"id":"msg_0","type":"message","role":"assistant","status":"completed",
         "content":[{"type":"output_text","text":"count()","annotations":[]}]}
      ]
    }
  }' | python -m json.tool
```

Notes:

- `GET /` returns `404` (expected). Use `.../docs` for the interactive API docs.
- Without a judge model configured, semantic stays `null` and execution uses a simple fallback:
  `5` if the query ran, `1` if it errored.

### Use with `logscale_evaluations` hill climb

If you also have the `logscale_evaluations` repo, you can run its hill-climb loop
against this Gym resources server (the loop will call `POST /verify`).

From the `logscale_evaluations` repo (while `ng_run` is running):

```bash
python hill_climb.py --iterations 1 --questions 2 --holdout 0 --engine gym \
  --gym-head-url http://127.0.0.1:11000 --gym-server-name logscale_cql \
  --output hill_climb_gym_results.json
```

### Example Output

```
============================================================
  LogScale Gym – Standalone Scoring Test
============================================================
  LogScale:  http://localhost:8080
  LLM Judge: ON (aws/anthropic/bedrock-claude-opus-4-6)

[..] Connecting to LogScale...
[OK] Repository: sandbox_zWlT5iVVpAnhkcUNRgSMb13J

── Score 1: Validity (LogScale validateQuery) ──────────
  [PASS] valid=1  count()
  [PASS] valid=1  ProcessRollup2 | count()
  [PASS] valid=1  #event_simpleName=ProcessRollup2 | groupBy(ImageFileName, fu
  [PASS] valid=1  groupBy(ComputerName, function=count()) | sort(_count, order
  [PASS] valid=1  hashRewrite(field="@ingesttimestamp", salt="salt1")
  [PASS] valid=0  | invalid_function(
  [PASS] valid=0  groupBy(( broken syntax
  [PASS] valid=0  count(field=,)

── Full Rubric (validity + semantic + execution) ───────

  [+] reward=1.000  Q: How many events are there in total?
      CQL: count()
      validity=1  semantic=5  execution=5
      semantic:  The query correctly returns the total number of events.
      execution: The query correctly returns the total count of events (25,000).

  [+] reward=0.933  Q: Show top 5 computers by event count
      CQL: groupBy(ComputerName, function=count()) | sort(_count, order=desc
      validity=1  semantic=5  execution=4
      semantic:  The query correctly groups by ComputerName, counts events, sorts descending.
      execution: Results show 5 computers with counts as expected, minor sort display issue.

  [+] reward=0.867  Q: Count ProcessRollup2 events
      CQL: #event_simpleName=ProcessRollup2 | count()
      validity=1  semantic=5  execution=3
      semantic:  The query correctly filters for ProcessRollup2 events and counts them.
      execution: Query is correct but results depend on data matching the tag field.

  [+] reward=0.933  Q: Show top 10 remote IPs for network connections
      CQL: NetworkConnectIP4 | groupBy(RemoteAddressIP4, function=count()) |
      validity=1  semantic=5  execution=4
      semantic:  The query correctly groups network connections by remote IP address.
      execution: Results show 10 remote IPs with counts as expected.

  [-] reward=0.000  Q: Count all events
      CQL: | broken syntax(
      validity=0  semantic=None  execution=None

============================================================
  LLM Judge: ON
  Repository: sandbox_zWlT5iVVpAnhkcUNRgSMb13J
============================================================
```

### What `setup.sh` Does

1. Starts `humio/humio-single-node-demo` container with `-p 8080:8080`
2. Waits for LogScale to become healthy (`/api/v1/status`)
3. Discovers the sandbox repository and its ingest token via GraphQL
4. Ingests `data/synthetic_falcon_events.csv` (5 000 synthetic Falcon events)
5. Verifies with a `count()` query

### How It Works

```
NeMo Gym Agent
    |
    |  POST /verify  { question, model response }
    v
LogScaleCQLResourcesServer  (app.py)
    |
    |  1. Extract CQL from model output
    |  2. Validate syntax (LogScale GraphQL validateQuery)   -> validity  0/1
    |  3. LLM judge: does query match user intent?           -> semantic  1-5
    |  4. Execute CQL, LLM judge: are results correct?       -> execution 1-5
    |  5. reward = (validity + semantic/5 + execution/5) / 3
    v
LogScale Container  (localhost:8080)
    Query Jobs REST API  +  GraphQL validateQuery
```

**Scoring rubric (3 components):**

| Component | Source | Range | Description |
|---|---|---|---|
| Validity | LogScale `validateQuery` | 0 or 1 | Is the CQL syntactically valid? |
| Semantic | LLM judge | 1-5 | Does the query capture the user's intent? |
| Execution | LLM judge | 1-5 | Do the results look correct? |

**Reward:** `(validity + semantic/5 + execution/5) / 3` (0.0 - 1.0)

Without a judge model configured, semantic defaults to the validity score and
execution uses binary success/failure (5 if runs, 1 if error).

### Configuration

Edit `configs/logscale_cql.yaml`:

```yaml
logscale_cql:
  resources_servers:
    logscale_cql:
      entrypoint: app.py
      domain: knowledge
      engine: container                  # "container" or "local"
      logscale_url: http://localhost:8080
      # repository: sandbox_xxx          # auto-detected if omitted
```

| Option | Default | Description |
|---|---|---|
| `engine` | `container` | `container` = live LogScale; `local` = pandas fallback |
| `logscale_url` | `http://localhost:8080` | LogScale instance URL |
| `repository` | auto-detected | Repository name (sandbox is discovered automatically) |
| `csv_path` | `data/synthetic_falcon_events.csv` | Path to CSV for ingestion / local mode |
| `judge_model_server` | — | Optional LLM judge (omit for binary reward) |

### Dataset Format

Each line in the JSONL dataset is:

```json
{
  "responses_create_params": {
    "input": [{"role": "user", "content": "Your NL question here"}]
  },
  "question": "Your NL question here",
  "metadata": {"category": "semantic", "difficulty": 2}
}
```

See `data/example.jsonl` for examples.

### Files

| File | Purpose |
|---|---|
| `setup.sh` | One-command setup: container + ingest + verify |
| `test_standalone.py` | Test all 3 scoring components without nemo_gym |
| `app.py` | Gym resources server (FastAPI) |
| `container_engine.py` | LogScale REST API client for query execution + validation |
| `configs/logscale_cql.yaml` | Server + agent configuration |
| `data/example.jsonl` | Example training dataset |
| `data/synthetic_falcon_events.csv` | 5 000 synthetic CrowdStrike Falcon events |

### Manual Container Management

```bash
# Start
docker run -d -p 8080:8080 --name=humio \
  --ulimit="nofile=250000:250000" \
  -e AUTHENTICATION_METHOD=none \
  humio/humio-single-node-demo

# Health check
curl http://localhost:8080/api/v1/status

# Stop / remove
docker rm -f humio
```
