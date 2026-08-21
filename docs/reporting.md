# Results, reports, comparisons, and sweeps

## Result layout

The `results/` directory is gitignored. A completed run persists a canonical E.17 score
JSON, sanitized transcript, initial/final snapshots, verified `run.json`, and a
compatible per-episode/model aggregate JSON. Writes are atomic and never silently
overwrite a completed artifact.

Run configuration records only allowlisted public settings: model ID, episode/world
pins, run kind, judge configuration, seed, budgets, and baseline state. Credentials and
credential-shaped fields are omitted from artifact bytes and configuration hashes.

## Offline reports

```sh
mirror-firm report results/my-run --output results/my-run/scorecard.html
mirror-firm compare results/model-a results/model-b --output results/compare.html
```

Reports render offline without a server or CDN. They escape model/document text and show
all required layers, overall, all-pass rate, CF identifiers, failed criteria/evidence,
cost, tokens, latency, steps, world days, configuration hash, and reliability metrics.
Unavailable reliability measures render as unavailable, not zero.

`compare` fails closed for malformed or incomplete results, incompatible public run
configuration, mixed world versions, reference/native mixing, contaminated input, and
duplicate artifacts. It is descriptive: it does not imply statistical significance.

## Separation rules

`reference (scripted) - not model performance` appears in a separate report section.
APEX results appear separately with their source revision and label, and cannot enter a
native aggregate, comparison, or headline metric. A result marked
`public-reference-answers` remains external and non-comparable.

For sweep configuration and five-run baseline requirements, see [running models](running-models.md).
