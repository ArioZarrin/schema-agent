# Part 4 outputs

All five outputs have matching CSV and Parquet files: `firmable_entities`, `entity_field_scores`, `entity_field_claims`, `entity_field_conflicts`, and `entity_conflict_values`.

The latest run used 6,000 gold observations and 3,959 accepted links, producing 3,415 profiles and 20,865 field claims. There are 1,437 fields with agreeing claims and 0 field conflicts. Database writes succeeded: True.

Claim C = R × L × F. Agreeing support is summed and capped at 1 for the profile. Conflicting values and their profile confidence stay null; each alternative gets its support divided by total conflict support. Future AI, data analyser and final decisions remain null.

Connections close after reads/saves and on failures. An idle project notebook connection can be released automatically; busy kernels and pending review are preserved. Exported inputs provide a fallback. If complete inputs are unavailable, the run pauses with a status and preserves prior results. `metrics.json` records the input mode and whether database persistence succeeded.
