# Senior Applied AI Engineer - Schema Inference

# Technical Assignment — Schema Inference and Entity Identification at Scale

---

## Before you start

**Time box: 6 hours.** Not 6 hours per day — 6 hours total. We would rather see a well-reasoned partial solution than a complete one that took a weekend. If you run out of time, stop, and tell us in the write-up what you would have done next. How you spend a fixed budget is part of what we are assessing.

**Window:** please return it within 7 days of receiving this. Tell us if that does not work and we will move the date.

**AI tools are expected, not tolerated.** Use Claude, GPT, Cursor, whatever you normally use. This task is about building a system that uses models to do work at scale; pretending you would build it by hand would tell us nothing. We do ask that you can explain every line you submit.

**The system itself has to be agentic.** This is the part candidates most often get wrong, so we are being explicit. Using Cursor to help you write a parser is not what we are asking for. We are asking you to build a multi-step agent that does the schema inference and mapping work itself, at runtime, on a source it has not seen before. The test is simple: if we handed your submission a seventh dataset tomorrow, could it produce a mapping config without you writing anything? If the answer is no, you have built the wrong thing, however good the code is. This applies to Part 2. Part 1 can be as deterministic as you like.

**Cost:** the task is designed to cost under USD $10 in API spend. Keep your receipts — we ask you to report actual spend. If you would rather not use paid APIs, a local model is completely acceptable and we will judge the result on the same terms.

---

## Context

Firmable maintains a resolved graph of Australian and New Zealand businesses — roughly 15 million companies today. The problem we are solving is that adding a new public data source currently costs an engineer several weeks: someone reads the schema, writes a connector, writes a transformer, and writes the logic that links each record to the right company.

There are thousands of useful public sources — company registers, building approvals, trade licences, workplace safety notices, court lists, procurement contracts. At an engineer-month per source, the ten-thousandth source is never worth loading. We think that maths changed. We want to find out whether adding a source can become **a review rather than a build**: a model infers the schema, proposes the mapping, and a human approves it.

This assignment is a small, real version of that problem.

Two things we care about more than most teams:

1. **A wrong link is worse than no link.** One bad merge quietly corrupts every answer built on top of it. We would always rather return nothing.
2. **Config beats code.** A solution that works for six sources because you wrote six parsers has not solved anything. A solution that works for six sources because you wrote one engine and six config files might.
3. **The configs have to be written by the agent, not by you.** One engine plus six hand-authored configs is the same problem as six parsers, moved sideways. The agent inspects the source, proposes the mapping, checks its own work, and emits the config. Your job is the system that does that, and the review step a human uses to accept or reject what it produces.

---

## The task

### Part 1 — Discovery and triage

`data.gov.au` exposes a CKAN Action API at `https://data.gov.au/data/api/3/action/` over a catalogue of 100,000+ datasets. No API key required. Useful actions: `package_search`, `package_show`, `resource_search`. State portals (`data.nsw.gov.au`, `data.qld.gov.au`, `data.vic.gov.au`, `data.sa.gov.au`) run the same API if you want more breadth.

**Deliverable:** from a pool of at least 200 catalogue records you retrieved programmatically, produce a **ranked shortlist of 50 datasets that plausibly contain identifiable Australian business entities.**

Rules:

- The shortlist must be produced by code, not by you reading titles. Hand-picking 50 datasets is a different and much easier task.
- Agentic triage is a bonus here, not a requirement. Cheap deterministic filtering ahead of any model call is a perfectly good answer at this stage, and if that is what you do, tell us why. The agentic requirement applies to Part 2.
- For each of the 50, output: dataset ID, title, publishing organisation, resource format(s), your confidence that it contains business entities, and a one-line reason.
- Tell us your false positive rate. Hand-check a random sample of 20 of your 50 and report how many actually contain business entities. We are more interested in this number being honest than in it being high.

Output as CSV or JSONL.

### Part 2 — Schema inference and canonical mapping

Choose **6 datasets** from your shortlist that you can actually download, and that are as different from each other as you can manage — different publishers, different formats, different shapes. At least one should not be a clean CSV.

Map each one to the canonical ontology in the attached `firmable_ontology.yaml`.

firmable_ontology.yaml

**Deliverable:** for each source, a **mapping config** — a machine-readable file that describes how to get from that source’s raw records to canonical observations. Plus **one generic extractor** that reads a config and produces output. Six configs and one engine; not six scripts.

**The configs must be generated by a multi-step agent.** A single prompt that swallows a sample and returns a mapping is not enough, and it will not hold up on the messier sources. We expect to see a pipeline with distinct steps, something along the lines of: inspect the resource and work out what it actually is, sample it, propose a mapping against the ontology, validate that proposal by running it over records it has not seen, and revise where it fails. How you decompose it is your call. What we want to see is state carried between steps, a model correcting its own output rather than being corrected by you, and a clear point at which a human is asked to approve.

Tell us the step count, the token cost and the wall-clock time for onboarding one source, and where a run can fail without losing the whole job.

Each mapping should record, per field: the source field it came from, the canonical field it maps to, any transformation applied, and your confidence in the mapping. Fields you could not map confidently should be left unmapped and listed, not guessed at.

Run the extractor over a sample of each source (1,000 records is plenty) and output canonical observations.

### Part 3 — Entity identification

Across the six sources you just processed, find businesses that appear in more than one of them.

The ontology file covers what an entity looks like. It deliberately says nothing about how entities relate to each other, or how you represent the fact that two records are the same business. That part is yours to design. Whether you model it as a cluster, a canonical key, a pairwise link table, a graph, or something else, we want to see the model you chose and the reasoning. Sources will also hint at relationships between different businesses, parent and subsidiary being the obvious one. Do something sensible with that or explain why you left it alone.

**Deliverable:**

- A set of proposed links: `{source_a_record, source_b_record, canonical_entity_key, confidence, evidence}`.
- An **unlinked queue**: records you believed contained a business but deliberately refused to link, and why. This is not a list of failures. We want to see where you drew the line.
- **Measured precision.** Hand-check 50 of your proposed links and report how many are correct. Tell us the threshold you used and what precision you got at that threshold. If you also want to report what happens at a looser threshold, we would find that interesting.

You do not need prior entity resolution experience to do this well. We are looking at how you reason about it, not whether you already know the standard techniques.

### Part 4 — Company profile

Take 50 of the entities you linked and produce a **company profile** for each: one record per business, merging what all six sources say about it.

**Deliverable:**

- One profile per entity, with a confidence on **each field**, not a single score for the whole profile. A profile can be highly confident about a name and barely confident about an address, and that has to be visible.
- Provenance per field: which source record the value came from.
- Where sources disagree, show your working. Which value won, why, and what the losing values were. Silently picking one is the failure mode we are looking for.
- Fields you could not fill should be absent, not blank and not guessed.
- A short note on what happens when a source is wrong. If we deleted one of your six sources, how many profiles change and how badly?

Format is up to you. One JSON object per entity is fine.

### Part 5 — Write-up

**Two pages maximum.** Bullets are fine. Cover:

1. **Cost.** What did it actually cost — dollars and tokens — to onboard one source? Break out the one-time inference cost from the per-record cost. If your answer is “I don’t know”, say so; if it is a guess, label it as one.
2. **Agent design.** How did you decompose the work into steps, and why that way? Where does the agent check itself, what happens when a step fails, and what does a human actually see when they are asked to approve? Tell us which step is the weakest and how you know.
3. **The hard one.** At least one of your six sources will have resisted. Which, why, and what would you do about it — noting that “write custom code for this one” is sometimes the correct answer, and we want to know how you decide when.
4. **Scale.** What breaks between 6 sources and 500? Be specific about which component fails first.
5. **Change.** Six months from now you improve the matching model. There are millions of existing links made by the old one. What do you do, and what do you need to have recorded in advance to be able to do it? This is the question we are most interested in.
6. **Next.** With three weeks instead of six hours, what would you build first?

Also tell us how you modelled entity relationships in Part 3 and why, in a few lines. We left that open on purpose and the choice tells us more than the implementation does.

---

## What to submit

A git repository (or a zip) containing:

- `README.md` — how to run it, from a clean checkout, in under 10 minutes
- Your code
- `WRITEUP.md` — Part 5
- Outputs: the shortlist, the six mapping configs, the canonical observations, the proposed links, the unlinked queue, the 50 company profiles
- Anything you used to hand-check precision

It must run. If something is broken, say so in the README rather than leaving us to find it.

---

## How we will assess it

Roughly in this order:

| **Is it agentic?** | Part 2 built as a multi-step system that maps a source it has not seen, or a person doing the mapping with AI help |
| --- | --- |
| **Does it generalise?** | One engine and six configs, or six scripts wearing a trench coat |
| **Mapping quality** | Correct, and honest about what it could not map |
| **Resolution judgment** | Sensible confidence, real abstention, measured precision |
| **Profile assembly** | Field-level confidence, visible conflict handling, provenance kept |
| **Measurement** | You know your own numbers and did not round them in your favour |
| **Engineering** | It runs, it is legible, it is reproducible |
| **Judgment** | The write-up |

Things that will count against a submission: mapping configs you wrote by hand; a single prompt doing the whole inference with no validation or revision step; per-source hand-tuned parsers; a single blended confidence score covering several different kinds of uncertainty; links reported without a precision number; 100% coverage claims; ignoring the licence terms attached to a dataset.

We are not scoring English. Write however you write.

---

## Questions

Ask them. Sending this task and then penalising you for clarifying it would be a strange thing for us to do. If something is ambiguous, either ask or make a decision and note it in the README — both are fine.