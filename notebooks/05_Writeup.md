# Part 5 — Write-up

## 1. Cost

I have more than 100 agent runs/threads from development and testing. After I added LangSmith, I could see the model calls, tokens, latency and failures much more clearly.

From my recent successful runs, onboarding one source with the normal agent costs around **15K tokens / ~$0.003 on average**. The normal model is `gpt-6-luna`.

I also built a **Turbo Agent** for difficult cases. It uses `gpt-6.1-sol` and is much more expensive. One test was about **30K tokens / $0.041**.

Part 1 embeddings are generated locally with SentenceTransformers, so there is no embedding API cost.

The important distinction is:

- **One-time inference cost:** understand the source and produce/validate the mapping config.
- **Per-record LLM cost after approval:** **$0**. The approved mapping is applied by the deterministic extractor.
- I did **not** measure infrastructure/compute cost per record, so I do not claim a number for that.

## 2. Agent design

Part 2 is a **multi-step LangGraph workflow**. I did not want one large agent prompt doing everything.

```text
dataset name / ID
→ resolve CKAN dataset
→ select resource
→ download + inspect
→ infer reader
→ load + profile
→ infer mapping + transformations
→ deterministic validation
→ bounded revision
→ human review
→ Gold extraction
```

I deliberately let **Python, Pandas and DuckDB** do as much deterministic work as possible: schema/datatype inspection, samples, identifier checks and validation. The model mainly handles the semantic work: which resource is useful, how fields map to the ontology, and what transformation is needed.

The agent checks itself after reader inference and mapping. Reader correction is bounded, and mapping revision is also bounded. If it still cannot produce a valid result, it does not loop forever.

At review time the Data Analyst sees the source sample, proposed mappings and transformations, field confidence, source reliability, validation result, Gold preview and candidate YAML. The choices are:

1. Approve
2. Reject
3. Edit manually
4. Improve with Turbo Agent

The Turbo path is a **Deep Agent** with a stronger model. It can inspect DuckDB, run Python in a sandbox and optionally use web research.

The weakest step is **source/schema interpretation**. Real public data has bad metadata, strange headers, encodings, archives and semantic ambiguity. This is also where most of my development failures happened, so LangSmith traces were useful for understanding what to improve.

## 3. The hard one

The hardest of my six sources was the **ABN Bulk Extract** because it is not a normal clean tabular source. It is distributed as large ZIP/XML data.

Instead of writing ABN-specific extraction code immediately, I extended the generic reader path: inspect ZIP members, inspect XML tags, identify the repeating record tag, stream the XML into a usable structure, then continue through the same profiling/mapping/extraction pipeline.

My rule for custom code is:

- if a generic reader/config can solve it safely, keep it generic;
- if a reusable improvement solves a whole class of sources, improve the framework;
- if a source is very valuable but genuinely unique, a custom adapter is acceptable;
- if the source simply has no useful entity-level data, reject it rather than force a mapping.

I also tested rejected sources such as **Annual Report CBS**. It exposed both reader/encoding issues and, more importantly, aggregate data that was not useful for entity resolution. Every failure has a lesson; sometimes the correct output is rejection.

## 4. Scale

This is an MVP, so going from **6 sources to 500** will expose many edge cases.

The component I expect to fail first is **Part 2 schema inference/onboarding**. Part 1, Part 3 and Part 4 are mostly deterministic once the algorithms are defined and are easier to batch or parallelise. Part 2 has a different source shape every time: encoding, CSV dialect, Excel layout, ZIP/XML, wrong metadata, large files, datatype problems and semantic ambiguity.

There is also a human-review scaling problem. Even a small escalation rate becomes expensive at 500 sources.

For production I would not keep DuckDB as the main execution layer. I would move the tables/pipeline to **Databricks/Spark**, or use a production platform such as **Snowflake**, depending on the environment.

The first real scale test I would run is simple: run the same pipeline over hundreds of datasets and record **where each one fails, retry count, human intervention, latency, tokens and cost**. That gives the real bottleneck instead of guessing.

## 5. Change

I separated **entity identity** from source observations.

`entities_pool` is intentionally small:

```text
entity_id
abn
acn
```

Source records are connected to entities through `entity_link_index`, with `match_method`, evidence and `link_confidence`.

If I improve the matching model six months later, I would **not overwrite millions of old links in place**. I would run a new linker version beside the old one, compare the results, measure what was added/removed/changed, review regressions, and only then promote the new version.

To make that possible I need to record in advance:

```text
linker_version
git_commit_hash
model/version if used
matching configuration + thresholds
match_method
match_evidence
link_confidence
source/input version
created_at
```

The Git commit is important because it points to the exact code, but it is not enough by itself if models, thresholds, configs or input snapshots can change.

Old links should remain available for audit, comparison and rollback.

### Entity relationships in Part 3

I modelled **record-to-entity identity links separately from entity-to-entity relationships**.

`entity_link_index` answers: *which entity does this source observation belong to?*

Actual business relationships, when a source contains them, should live in a separate relationship edge structure such as:

```text
subject_entity_id
relationship_type
object_entity_id
source/provenance
confidence
```

I kept them separate because identity resolution and business relationships are different claims. The six selected sources did not justify inventing relationship edges, so I did not manufacture them.

## 6. Next

With three weeks, I would first move the data layer toward production: **Databricks/Spark or Snowflake instead of local DuckDB**, then harden the deterministic Parts **1, 3 and 4**.

After that I would define proper **rubrics and regression tests**. Changing agents without a fixed evaluation set is improving blindly.

Then I would run around **100 representative datasets** end-to-end and measure ranking quality, onboarding success, mapping quality, failures, human intervention, latency, tokens and cost.

Finally I would optimize the whole system from those results:

- tune Part 1 ranking weights and add useful scorers;
- improve reader/schema inference from real failures;
- reduce unnecessary model/tool calls using LangSmith;
- improve and version the linker;
- complete proper precision evaluation for Part 3;
- improve Part 4 reconciliation rules.

The main principle would stay the same: **measure failures, learn from them, then improve the system based on evidence.**
