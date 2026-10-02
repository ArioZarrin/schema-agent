# Agentic Schema Inference and Entity Resolution

## Overview

This project builds an end-to-end pipeline for discovering Australian public datasets, mapping heterogeneous source schemas into a common Firmable ontology, resolving source records to business identities, and producing reconciled entity profiles with provenance and confidence.

The system therefore separates four different problems instead of collapsing them into one large “company table”:

```text
Part 1 — Which datasets are likely to contain useful business entities?
    ↓
Part 2 — What does each source actually claim?
    ↓
Part 3 — Which source observations refer to the same entity?
    ↓
Part 4 — What can we safely say about that entity?
```

---

## Architecture

```text
data.gov.au / CKAN
        │
        ▼
┌──────────────────────────┐
│ Part 1: Dataset Discovery│
│ keyword + local embedding│
└────────────┬─────────────┘
             │ Top 50
             ▼
┌───────────────────────────────┐
│ Part 2: LangGraph Onboarding  │
│                               │
│ resolve dataset/resource      │
│ inspect file                  │
│ infer reader                  │
│ profile source                │
│ infer mapping/transformation  │
│ deterministic validation      │
│ human approval                │
└──────────────┬────────────────┘
               │
        src_*  │  gold_*
               ▼
┌───────────────────────────────┐
│ Part 3: Entity Resolution     │
│ ABN/ACN first                 │
│ conservative field matching   │
└──────────────┬────────────────┘
               │
       entities_pool
       entity_link_index
               │
               ▼
┌───────────────────────────────┐
│ Part 4: Entity Profiles       │
│ claims + provenance           │
│ R × L × F confidence          │
│ agreement/conflict handling   │
└──────────────┬────────────────┘
               │
               ▼
        firmable_entities
```

---

# Part 1 — Dataset Discovery and Ranking

The discovery stage uses the `data.gov.au` CKAN API and deliberately combines lexical and semantic ranking.

The run in the notebook:

- CKAN reported **5,689 matches**.
- The pipeline retrieved **1,000 catalogue records**.
- Format filtering left **457 datasets with usable resources**.
- The top **50** were saved to `outputs/part1/ranked_shortlist_50.csv`.

## Hybrid ranking

Keywords are extremely important in this domain. Terms such as `ABN`, `ACN`, `company name`, `business name`, `licence holder`, `supplier` and `address` are strong direct signals.

I therefore use a weighted keyword scorer, including negative signals such as `statistics`, `aggregate`, `business counts` and `survey`.

However, keyword-only search misses datasets whose metadata uses different wording but is still semantically related. To recover those sources, I also calculate a local embedding score using:

```text
SentenceTransformer("all-MiniLM-L6-v2")
```

The current hybrid score is:

```text
0.60 × normalized keyword score
+
0.40 × semantic embedding score
```

The embedding model runs locally, so this stage has **no embedding API cost**.

The `0.60 / 0.40` weighting is currently a design choice rather than a learned optimum. With a larger manually labelled dataset, these weights and additional ranking features could be trained or tuned as a small supervised ranking/regression problem.

## Manual quality check

A reproducible random sample of 20 records from the Top 50 was manually reviewed.

Three were business-related but aggregate rather than identifiable entity datasets:

- Business establishments per CLUE industry in municipality
- Annual Report CBS
- Business establishments and jobs data by business size and CLUE industry in municipality

Observed false-positive rate:

```text
3 / 20 = 15%
```

This is also a useful lesson: metadata relevance and entity-level usefulness are not the same thing.

---

# Part 2 — Agentic Schema Inference

Part 2 is implemented as a **LangGraph multi-step workflow**, not as one unrestricted LLM prompt.

The normal workflow is intentionally structured and bounded:

```text
dataset name / ID
    ↓
resolve against Part 1 shortlist
    ↓
read CKAN metadata
    ↓
select resource
    ↓
download + inspect files
    ↓
infer reader configuration
    ↓
load source
    ↓
profile source + deterministic identifier checks
    ↓
generate ontology mapping + transformations
    ↓
deterministic validation
    ↓
bounded revision if required
    ↓
human review
    ↓
approve / reject / edit / Turbo
    ↓
generic extraction into gold_<dataset>
```

## Why split the workflow?

The LLM is useful for semantic interpretation, but many tasks are safer and cheaper as deterministic code.

### Deterministic/code responsibilities

- CKAN retrieval
- file downloading and inspection
- schema and datatype profiling
- source sampling
- ABN/ACN validation
- ontology validation
- SQL-expression safety checks
- extraction
- persistence
- metrics

### Model responsibilities

- resource selection
- reader interpretation
- semantic field mapping
- transformation proposal
- difficult-source investigation

This keeps the model focused on the parts that actually require semantic reasoning.

## Normal agent

The normal model is configured as:

```text
openai:gpt-6-luna
```

It uses strict Pydantic structured outputs for:

- resource selection
- reader selection
- mapping generation

The mapping schema supports these transformation types:

```text
direct
sql_expression
constant
record_hash
```

Each field mapping also records:

- source field(s)
- target Firmable field
- transformation
- field confidence
- derivation level `L0–L4`
- reasoning

Fields that cannot be mapped are explicitly listed under `unmapped_fields`. The system should prefer saying “this field does not map” rather than inventing an ontology value.

## Reader support

The generic reader supports:

```text
CSV
TSV
XLS
XLSX
JSON
JSONL
XML
ZIP-contained resources
```

The reader agent decides settings such as delimiter, header, skipped rows, sheet name and XML record tag.

The normal path is bounded:

- reader: initial attempt + **1 revision**
- mapping: initial attempt + up to **2 revisions**

If deterministic validation still fails, control returns to the human instead of allowing an uncontrolled agent loop.

## Human-in-the-loop governance

LangGraph `interrupt()` is used for review.

The analyst sees:

- selected source/resource
- reader configuration
- source reliability
- source sample
- proposed source → ontology mappings
- transformation type
- mapping confidence
- Gold preview
- validation errors/warnings
- unmapped fields
- candidate YAML

The analyst then chooses:

```text
1. Approve
2. Reject
3. Edit Manually
4. Improve with Turbo Agent
```

Approved mappings are persisted and extracted. Rejected candidates are also preserved rather than silently discarded.

## Turbo Agent

Difficult cases can be escalated to a stronger model:

```text
openai:gpt-6.1-sol
```

The Turbo path is implemented as a **Deep Agent** using `create_deep_agent(...)`.

Its tools include:

- querying the source in DuckDB
- executing Python in the sandbox
- validating/submitting a candidate mapping
- optional Tavily web research

Turbo is intentionally an escalation path rather than the default because it is significantly more expensive.

## State, recovery and observability

The LangGraph workflow uses:

- `StateGraph`
- persistent SQLite checkpointing through `SqliteSaver`
- interrupt/resume semantics
- per-dataset state files
- per-dataset metrics
- LangSmith tracing when configured

Part 2 tracks metrics including:

```text
model calls
tool calls
validation calls
step count
input/output/total tokens
wall-clock time
revision count
estimated cost where configured
LangSmith project/tracing state
```

Recent LangSmith traces showed normal onboarding around **15K tokens / ~$0.003** for a typical source. One Turbo test was approximately **30K tokens / $0.041**.

The important cost property is that this is primarily a **one-time schema inference cost**. Once a mapping is approved, the generic extractor applies it deterministically, so there is **no LLM cost per record**.

## Six selected sources

The final six sources were:

1. ASIC - Company Dataset
2. ABN Bulk Extract
3. Victorian Government Schools ABNs
4. ACNC Registered Charities
5. Victorian liquor licences by location
6. Business establishments location and industry classification

The generic extractor created six Gold tables, each with **1,000 rows**, for a total of **6,000 standardized source observations**.

Gold is deliberately an observation layer. It records what a source claims, not a final Firmable truth.

---

# Part 3 — Entity Resolution

Part 3 reads the six Gold tables and creates an identity layer without modifying the original Gold observations.

## Identity registry

`entities_pool` is intentionally minimal:

```text
entity_id
abn
acn
```

A valid stated ABN becomes the entity identity.

For ACN-only observations, the implementation validates the ACN and searches for ABN-checksum-compatible two-digit prefixes. It only derives an ABN-style identity when the checksum result is unambiguous. This does **not** claim that a registered ABN was stated by a source; the `abn` field remains null unless one was actually observed.

## Link registry

Accepted source-record → entity links are stored in:

```text
entity_link_index
```

with:

```text
link_id
source_id
source_record_id
entity_id
match_method
match_evidence
link_confidence
created_at
```

A source record can have only one accepted entity link.

## Resolution strategy

Resolution starts with deterministic identifiers:

```text
valid ABN
→ exact identity
valid ACN
→ existing ACN identity if known
→ otherwise unambiguous derived identity
```

Records without identifiers can then be compared against evidence from already-linked observations using:

- legal name
- trading name
- website
- address
- locality
- state
- postcode
- country

The matcher is deliberately conservative. Automatic field-based linking requires:

```text
minimum score = 0.97
minimum winner margin = 0.02
no detected conflict
```

Possible matches that do not meet the acceptance rule are kept separately from accepted links.

## Current Part 3 results

From **6,000 Gold observations**:

```text
ABN exact              3,909
ACN-derived identity      27
ACN exact                 23
--------------------------------
Accepted links          3,959
Entities                3,415
Unresolved              2,041
```

Unresolved records:

```text
ABN/ACN conflict          14
No candidate           2,027
```

The current sample produced **0 accepted field-based inferred links**. The matcher produced **22 possible-match proposals for 12 records**, but correctly kept them outside `entity_link_index`.

This is intentional abstention, not a hidden success claim.

A reproducible sample of **50 accepted links** is generated for manual precision review. In the saved notebook only **1 of 50** had been labelled, so the project should not claim a statistically meaningful measured precision yet.

---

# Part 4 — Entity Profiles and Conflict Handling

Part 4 answers a different question from Part 3.

Part 3 asks:

> Which observations belong to the same entity?

Part 4 asks:

> Given those linked source claims, what value can Firmable safely expose for each field?

Every linked non-null field becomes an immutable claim with provenance.

## Claim confidence

For a field claim:

```text
C_claim = R × L × F
```

where:

- `R` = source reliability
- `L` = entity link confidence
- `F` = Part 2 field-mapping confidence

A reliable dataset, a strong entity link and a high-confidence field mapping therefore produce stronger entity evidence.

## Agreement

Claims are normalized for comparison while preserving their original values and provenance.

If all claims for an entity/field normalize to the same value, their support is combined:

```text
C_final = min(1, Σ claim_score)
```

Independent agreeing observations therefore reinforce one another.

## Conflict

If multiple different normalized values remain for the same entity/field:

- the field is marked as conflicting
- no winner is silently selected
- the profile field and confidence remain null
- all claims are preserved
- all alternatives and their support are preserved
- later analyst/rule-based reconciliation can make a decision

This keeps disagreement visible rather than hiding it inside a “best guess”.

## Current Part 4 results

```text
Gold observations             6,000
Accepted linked observations  3,959
Entities                      3,415
Field claims                 23,503
Uncontested fields           19,718
  single-claim fields        18,039
  agreeing multi-claims       1,679
Final entity profiles         3,415
Real conflicts                    0
```

The notebook includes a separate in-memory worked conflict example. It is illustrative only and is not presented as a real conflict from the six-source run.

Five Part 4 outputs are produced:

```text
firmable_entities
entity_field_scores
entity_field_claims
entity_field_conflicts
entity_conflict_values
```

They are saved to DuckDB and exported to Parquet/CSV.

---

# Data Model

```text
src_<dataset>
    │
    │ original source structure
    ▼
gold_<dataset>
    │
    │ normalized source observations
    ▼
entity_link_index ───────► entities_pool
    │                       entity identity
    │
    ▼
entity_field_claims
    │
    ├──► entity_field_scores
    ├──► entity_field_conflicts
    └──► entity_conflict_values
              │
              ▼
       firmable_entities
```

The most important boundary is:

> **Gold stores source claims. Part 3 assigns identity. Part 4 builds a profile.**

---

# Outputs

```text
outputs/
├── part1/
│   └── ranked_shortlist_50.csv
│
├── part2/
│   ├── raw/
│   ├── dataset_profiles/
│   ├── review/
│   ├── mapping_configs/
│   ├── rejected/
│   ├── state/
│   ├── metrics/
│   ├── sandbox/
│   └── workflow_checkpoints.sqlite
│
├── part3/
│   ├── entities_pool.parquet
│   ├── entity_link_index.parquet
│   ├── entity_resolution_review.parquet
│   ├── possible_matches.csv
│   ├── link_review_50.csv
│   ├── source_coverage.csv
│   ├── resolution_passes.csv
│   └── resolution_metrics.json
│
└── part4/
    ├── firmable_entities.*
    ├── entity_field_scores.*
    ├── entity_field_claims.*
    ├── entity_field_conflicts.*
    ├── entity_conflict_values.*
    └── metrics.json
firmable.duckdb
```

---

# Failure Lessons

A useful part of building this pipeline was examining failures rather than hiding them.

### Aggregate sources

`Annual Report CBS` can be parsed and mapped at an observation level, but it contains aggregate statistics rather than useful entity-level records.

The correct outcome is to reject it for entity onboarding rather than manufacture entity mappings.

### Encoding

A CBS CSV also exposed a non-UTF-8 reader failure. The reader retry logic exists, but encoding is not currently a first-class `ReaderConfig` option. Production hardening should add automatic encoding detection/recovery rather than relying on `ignore_errors`. 

### Entity matching

The current six-source 1,000-row samples do not provide enough cross-source overlap for the conservative field matcher to accept any identifier-less matches. This is a useful limitation to expose. A larger/full-source test is needed before judging recall.

### Manual precision

The 50-link audit exists, but it is not complete in the saved notebook. Precision should only be reported after enough links have actually been reviewed.

---

# What I Would Build Next

With more time I would focus on measurement and production hardening before adding more agent complexity.

## 1. Build a regression/evaluation suite

Define rubrics and run at least ~100 representative datasets and measure:

```text
ranking quality
reader success rate
mapping quality
failure stage
retry count
human intervention rate
latency
tokens
cost
```

This gives a stable benchmark for every future prompt/model/tool change.

## 2. Tuning and ranking

For example the current `0.60 keyword / 0.40 embedding` weighting works reasonably well, but it is hand-set.

With labelled examples I would learn/tune:

- keyword weight
- embedding weight
- publisher signal
- resource-format signal
- entity-level-field evidence
- negative aggregate/statistical signals

A small supervised model could have a large impact on the number of useful sources reaching Part 2.

## 3. Harden source onboarding

Priority reader improvements:

- encoding detection
- broken CSV recovery
- more robust Excel/header inference
- archive handling
- large-file sampling
- stronger source-suitability gating

## 4. Version entity linking

Millions of links cannot be safely overwritten when the matcher changes.

Future link provenance should include at least:

```text
linker_version
git_commit_hash
model/version if applicable
matching configuration
thresholds
match_method
match_evidence
link_confidence
source/input version
created_at
```

A new linker version should be replayed alongside the previous one and compared before promotion.

## 5. Production data platform

DuckDB is appropriate for this local MVP and reproducible analysis. At larger scale/concurrency, I would move the execution/storage layer to a production data platform such as **Databricks/Spark or Snowflake**, while keeping the same logical separation between observations, identity and reconciliation.

---

# Technology

Main technologies used:

```text
Python
Pandas
DuckDB
data.gov.au CKAN API
SentenceTransformers
LangGraph
LangChain
Deep Agents
OpenAI models
LangSmith
Pydantic
Tavily (Turbo only)
Parquet / CSV / YAML / JSON
```

---

# Development Approach

I used AI tools, including Codex, as coding/refactoring assistants during implementation. I kept architecture and evaluation decisions explicit in the notebooks: hybrid discovery, bounded agent steps, human approval, the observation/identity/profile separation, conservative linking, and the confidence/conflict model.

The project deliberately uses models where semantic reasoning adds value and deterministic code where correctness should be testable.

---

# How to Run

The project uses [`uv`](https://docs.astral.sh/uv/) for Python environment and dependency management.

## 1. Install `uv`

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Verify the installation:

```bash
uv --version
```

## 2. Install Dependencies

From the project root:

```bash
uv sync
```

The project dependencies are defined in `pyproject.toml` and locked in `uv.lock`.

## 3. Configure Environment Variables

Create a local `.env` file from the provided example:

```bash
cp .env.example .env
```

At minimum, set:

```env
OPENAI_API_KEY=YOUR_OPENAI_API_KEY_HERE
```

Optional integrations:

```env
# Optional: web research for the Turbo Agent
TAVILY_API_KEY=YOUR_TAVILY_API_KEY_HERE
# Optional: LangSmith tracing and observability
LANGSMITH_API_KEY=YOUR_LANGSMITH_API_KEY_HERE
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=firmable-entities
```

Only `OPENAI_API_KEY` is required for the core pipeline.

Without Tavily, the Turbo Agent still works but does not have external web search.

Without LangSmith, the pipeline still works but tracing and LangSmith observability are disabled.

## 4. Start Jupyter

From the project root:

```bash
uv run --with jupyter jupyter lab
```

Then run the notebooks in order:

```text
notebooks/
├── 01_dataset_discovery.ipynb
├── 02_schema_inference.ipynb
├── 03_entity_resolution.ipynb
└── 04_entity_profiles.ipynb
```

### `01_dataset_discovery.ipynb`

Retrieves datasets from the `data.gov.au` CKAN API, applies the keyword and semantic ranking pipeline, and creates:

```text
outputs/part1/ranked_shortlist_50.csv
```

### `02_schema_inference.ipynb`

Runs the LangGraph multi-step schema inference and mapping workflow.

For each dataset the agent:

```text
resolves dataset/resource
→ inspects the source
→ infers the reader
→ profiles the data
→ proposes mappings and transformations
→ validates them
→ revises failures
→ requests human review
→ extracts approved mappings to Gold
```

At the human review step:

```text
1 - Approve
2 - Reject
3 - Edit Manually
4 - Improve with Turbo Agent
```

Approved configurations are written to:

```text
outputs/part2/mapping_configs/
```

and standardized observations are loaded into `gold_*` tables in DuckDB.

### `03_entity_resolution.ipynb`

Reads the Gold observations and performs entity identification and linking.

Main outputs are written to:

```text
outputs/part3/
```

including:

```text
entities_pool.parquet
entity_link_index.parquet
entity_resolution_review.parquet
possible_matches.csv
link_review_50.csv
resolution_metrics.json
```

### `04_entity_profiles.ipynb`

Builds entity-level profiles from the linked observations, including field-level confidence, provenance, agreement and conflict handling.

Outputs are written to:

```text
outputs/part4/
```

including:

```text
firmable_entities
entity_field_scores
entity_field_claims
entity_field_conflicts
entity_conflict_values
```

in CSV/Parquet form.


## Runtime Files

Running the notebooks creates local runtime artifacts including:

```text
firmable.duckdb
outputs/part2/raw/
outputs/part2/workflow_checkpoints.sqlite
```

Downloaded raw data, local databases, checkpoints and other reproducible runtime files are excluded from Git.

The final outputs required for assessment are kept under `outputs/`.


## Final Note

I relocated my existing `outputs/` directory to `outputs_ario/` so your run starts from a fresh environment.

If a dataset has already been approved, the workflow will prompt you for confirmation before running it again.

If you have any problems running the project, please contact me at **Ario.Zarrin@gmail.com**.