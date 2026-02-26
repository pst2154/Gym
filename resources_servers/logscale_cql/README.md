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

When you see `"status":"ok"`, LogScale is running.

**First-time setup (required):** Open http://localhost:8080 in your browser.
LogScale will show an initial setup screen where you need to enter a
license key. Paste the key into the UI and click through the setup wizard.
This only needs to be done once — the key persists across container restarts.
If you need the key, contact asteiner@nvidia.com.

Once you've completed the UI setup, the APIs (GraphQL, query jobs, ingest)
become available. The rest of this guide uses those APIs.

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
INFERENCE_API_KEY=your-api-key-here
```

The server reads this file on startup. The default model is
`aws/anthropic/bedrock-claude-opus-4-6` via `https://inference-api.nvidia.com`.
You can override these with environment variables:

| Variable | Default | What it does |
|---|---|---|
| `INFERENCE_API_KEY` | (required) | API key for the LLM judge |
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
export REPO="sandbox_zWlT5iVVpAnhkcUNRgSMb13J"   # your repo name from step 3
export TOKEN="your-ingest-token-from-step-3"

python3 -c "
import csv, json, os, requests

repo = os.environ['REPO']
token = os.environ['TOKEN']

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
            'Authorization': f'Bearer {token}',
        },
    )
    print(f'  Ingested {min(i+500, len(rows))}/{len(rows)}  (HTTP {resp.status_code})')
"
```

Verify the data is there (uses `$REPO` from above):

```bash
curl -s "http://localhost:8080/api/v1/repositories/${REPO}/queryjobs" \
  -H 'content-type: application/json' \
  -d '{"queryString":"count()","start":"7days","isLive":false}' \
  | python3 -m json.tool
```

Or just run `setup.sh` which does steps 1-4 automatically:

```bash
bash setup.sh
```

> **Note:** When running via `ng_run`, the server calls `setup.sh`
> automatically during startup. You do **not** need to run it manually
> in that case — the container will be started and data ingested before
> the server begins accepting requests.

### 5. Install the Gym

From the Gym repo root:

```bash
cd Gym
uv venv && uv pip install -e .
```

This installs `nemo_gym` and the `ng_run` CLI.

### 6. Start the Gym Server

You need the logscale_cql config **and** a model config. Use `vllm_model`
(not `openai_model`) because the NVIDIA inference API only supports
`/chat/completions`, not the OpenAI `/responses` endpoint. The vllm_model
server translates Responses API calls into chat completions automatically.

```bash
cd Gym
source .venv/bin/activate

export POLICY_BASE_URL=https://inference-api.nvidia.com/v1
export POLICY_API_KEY=<your-inference-api-key>
export POLICY_MODEL_NAME=nvidia/nvidia/nemotron-3-super-preview

ng_run "+config_paths=[resources_servers/logscale_cql/configs/logscale_cql.yaml,responses_api_models/vllm_model/configs/vllm_model.yaml]" \
  +policy_base_url=$POLICY_BASE_URL +policy_api_key=$POLICY_API_KEY +policy_model_name=$POLICY_MODEL_NAME
```

Wait ~45 seconds until you see all 3 servers ready:
- **Head server** on port 11000 — manages server instances
- **logscale_cql resources server** — does scoring/verification
- **logscale_cql_simple_agent** — routes questions to the policy model
- **policy_model** (vllm_model) — calls the LLM

Find the server URLs:

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

`configs/logscale_cql.yaml` defines the resources server **and** a simple
agent with train/validation/example datasets:

```yaml
logscale_cql_resources_server:
  resources_servers:
    logscale_cql:
      entrypoint: app.py
      domain: knowledge
      engine: container
      logscale_url: http://localhost:8080
      judge_model_server:
        type: responses_api_models
        name: policy_model
      judge_responses_create_params:
        input: []

logscale_cql_simple_agent:
  responses_api_agents:
    simple_agent:
      resources_server:
        type: resources_servers
        name: logscale_cql_resources_server
      model_server:
        type: responses_api_models
        name: policy_model
      datasets:
      - name: train
        type: train
        jsonl_fpath: resources_servers/logscale_cql/data/train.jsonl
        gitlab_identifier:
          dataset_name: logscale_cql
          version: 0.0.1
          artifact_fpath: train.jsonl
        license: NVIDIA Internal Use Only, Do Not Distribute
      - name: validation
        type: validation
        jsonl_fpath: resources_servers/logscale_cql/data/validation.jsonl
        gitlab_identifier:
          dataset_name: logscale_cql
          version: 0.0.1
          artifact_fpath: validation.jsonl
        license: NVIDIA Internal Use Only, Do Not Distribute
      - name: example
        type: example
        jsonl_fpath: resources_servers/logscale_cql/data/example.jsonl
```

| Option | Default | Description |
|---|---|---|
| `engine` | `container` | `container` = live LogScale; `local` = pandas fallback |
| `logscale_url` | `http://localhost:8080` | Where LogScale is running |
| `repository` | auto-detected | Set manually if you have multiple repos |

### Benchmark a Model

To benchmark a model on all questions (train + validation combined):

```bash
# Combine datasets into one file
cat resources_servers/logscale_cql/data/train.jsonl \
    resources_servers/logscale_cql/data/validation.jsonl \
    > resources_servers/logscale_cql/data/all_questions.jsonl

# Collect rollouts (Gym must be running from step 6)
# max_output_tokens is important for reasoning models that use thinking tokens.
ng_collect_rollouts \
    +agent_name=logscale_cql_simple_agent \
    +input_jsonl_fpath=resources_servers/logscale_cql/data/all_questions.jsonl \
    +output_jsonl_fpath=resources_servers/logscale_cql/data/benchmark_rollouts.jsonl \
    +num_samples_in_parallel=10 \
    "+responses_create_params={max_output_tokens: 32768}"
```

View results:

```bash
ng_viewer +jsonl_fpath=resources_servers/logscale_cql/data/benchmark_rollouts.jsonl
```

To benchmark a different model, change `POLICY_MODEL_NAME` (and
optionally `POLICY_BASE_URL`), restart `ng_run`, and re-run the
`ng_collect_rollouts` command above.

### Datasets

Datasets are stored in the GitLab Model Registry under `logscale_cql`
version `0.0.1`. To download them for `ng_prepare_data`:

```bash
ng_prepare_data \
    "+config_paths=[resources_servers/logscale_cql/configs/logscale_cql.yaml,responses_api_models/vllm_model/configs/vllm_model.yaml]" \
    +output_dirpath=data/logscale_cql \
    +mode=train_preparation \
    +should_download=true \
    +data_source=gitlab
```

This requires GitLab credentials in `env.yaml` at the Gym root:

```yaml
mlflow_tracking_uri: https://gitlab-master.nvidia.com/api/v4/projects/241584/ml/mlflow
mlflow_tracking_token: <your-gitlab-api-token>
```

### Customer Evals

This server is registered in `scripts/run_customer_evals.py`. To run evals:

```bash
python scripts/run_customer_evals.py \
    ++model_short_name_for_upload=nemotron-nano-9b-v2 \
    ++policy_api_key=<your-key>
```

### Baselines

Baseline results on the validation set (10 questions, no-judge mode — validity + execution only):

| Model | Mean Reward | Validity Rate | Notes |
|---|---|---|---|
| test_standalone.py (hardcoded CQL) | 0.667 | 80% (4/5) | Manually written queries, 1 intentionally invalid |

With LLM judge enabled (INFERENCE_API_KEY set, `bedrock-claude-opus-4-6`):

| Model | Mean Reward | Validity | Semantic (avg) | Execution (avg) |
|---|---|---|---|---|
| Baseline pending | — | — | — | — |

To generate your own baselines, run the benchmark or customer evals:

```bash
python scripts/run_customer_evals.py \
    ++model_short_name_for_upload=<your-model> \
    ++policy_api_key=<key>
```

Results are written to `resources_servers/logscale_cql/data/<model>_validation_rollouts.jsonl`.

### Files

| File | What it is |
|---|---|
| `setup.sh` | Automates container start + data ingest (runs automatically via `ng_run`) |
| `app.py` | The Gym resources server (FastAPI) with Ray SPREAD scheduling |
| `container_engine.py` | Talks to LogScale (query execution + validation) |
| `configs/logscale_cql.yaml` | Server + agent + dataset configuration |
| `prompt_templates/` | Judge and question-generation prompts (text files) |
| `data/synthetic_falcon_events.csv` | 5000 synthetic CrowdStrike Falcon events |
| `data/example.jsonl` | 5 example questions |
| `data/train.jsonl` | Training questions (download via `ng_prepare_data`) |
| `data/validation.jsonl` | Validation questions (download via `ng_prepare_data`) |
| `data/all_questions.jsonl` | Combined train+validation for benchmarking |
| `data/benchmark_rollouts.jsonl` | Output from `ng_collect_rollouts` |
