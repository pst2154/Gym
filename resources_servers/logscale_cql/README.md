## LogScale CQL Gym Resources Server

A standalone NeMo Gym environment that evaluates natural-language-to-CQL
queries against a live LogScale container. It generates evaluation questions,
validates CQL syntax, executes queries, and uses an LLM judge to score results.

### What It Does

```
                      POST /generate_questions
                             |
                             v
POST /verify ------> LogScaleCQLResourcesServer (app.py)
                             |
                      1. Extract CQL from model output
                      2. Validate syntax (LogScale GraphQL)     -> validity  0/1
                      3. Execute CQL against LogScale container
                      4. LLM judge: does query match intent?    -> semantic  1-5
                      5. LLM judge: are results correct?        -> execution 1-5
                      6. reward = (validity + sem/5 + exec/5) / 3
                             |
                             v
                      LogScale Container (localhost:8080)
```

### Prerequisites

- Docker running
- Python >= 3.9
- `SONNET_46_KEY` in `~/.env` (your NVIDIA API key for the LLM judge)

### Setup (one time)

```bash
cd Gym
uv venv && uv pip install -e .
```

### Run the Full Pipeline

From the repo root, paste each block one at a time:

**Step 1: Start LogScale and ingest data (~2 min first time)**

```bash
bash Gym/resources_servers/logscale_cql/setup.sh
```

This starts a LogScale Docker container, waits for it to be healthy,
discovers the sandbox repository, ingests 5000 synthetic Falcon events,
and verifies with a count() query.

**Step 2: Start the Gym server**

```bash
cd Gym && source .venv/bin/activate
ng_run "+config_paths=[resources_servers/logscale_cql/configs/logscale_cql.yaml]" &
```

Wait ~30s until you see "All 1 / 1 servers ready". Then find the server URL:

```bash
curl -s http://127.0.0.1:11000/server_instances | python3 -m json.tool
```

Note the `url` field (e.g. `http://127.0.0.1:18449`). Set it:

```bash
GYM_URL="http://127.0.0.1:18449"
```

**Step 3: Generate evaluation questions**

```bash
curl -s "$GYM_URL/generate_questions" \
  -H 'content-type: application/json' \
  -d '{"count":25,"difficulty":2}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); json.dump(d['questions'],open('generated_questions.json','w'),indent=2); print(f'Generated {len(d[\"questions\"])} questions')"
```

The Gym queries LogScale for field names and event types, then asks
the LLM to generate questions. Output: `generated_questions.json`.

**Step 4: Run the hill climb evaluation**

```bash
cd /path/to/Logscale_Container_Eval
python logscale_evaluations/hill_climb.py \
  --iterations 5 --questions 5 --holdout 20 \
  --engine gym --gym-head-url http://127.0.0.1:11000 \
  --gym-server-name logscale_cql \
  --questions-file generated_questions.json \
  --difficulty auto --output hill_climb_gym_results.json
```

Output: `hill_climb_gym_results.json` and `hill_climb_gym_results.prompt.txt`.

**Cleanup**

```bash
kill %1                  # stop Gym (background job)
docker stop humio        # stop LogScale
```

### API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/generate_questions` | POST | Generate evaluation questions from live data |
| `/verify` | POST | Score a CQL query (validate + execute + LLM judge) |
| `/docs` | GET | Interactive API docs |

### Scoring Rubric

| Component | Source | Range | Description |
|---|---|---|---|
| Validity | LogScale `validateQuery` | 0 or 1 | Is the CQL syntactically valid? |
| Semantic | LLM judge | 1-5 | Does the query capture the user's intent? |
| Execution | LLM judge | 1-5 | Do the results look correct? |

**Reward formula:** `(validity + semantic/5 + execution/5) / 3` = 0.0 to 1.0

Without `SONNET_46_KEY`, semantic defaults to `null` and execution uses
binary pass/fail (5 if it ran, 1 if it errored).

### Configuration

Edit `configs/logscale_cql.yaml`:

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
| `logscale_url` | `http://localhost:8080` | LogScale instance URL |
| `repository` | auto-detected | Sandbox repo discovered automatically |

### Files

| File | Purpose |
|---|---|
| `setup.sh` | Start container + ingest data + verify |
| `app.py` | Gym resources server (FastAPI) |
| `container_engine.py` | LogScale REST API client |
| `configs/logscale_cql.yaml` | Server configuration |
| `data/synthetic_falcon_events.csv` | 5000 synthetic CrowdStrike Falcon events |
| `data/example.jsonl` | Example training dataset |
