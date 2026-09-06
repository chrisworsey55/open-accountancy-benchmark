# Public learning contract v0.1

`mirrorfirm.learning.schemas` ships strict Pydantic definitions and JSON Schema exports
for `ProductionTrace`, `PractitionerCorrection`, `DifferenceRecord`, `FailureCluster`,
`ImprovementTask` and `TargetedEvalRef`. The example below is entirely fictional.

```python
from mirrorfirm.learning.schemas import emit_synthetic_trace, HumanEffort

trace = emit_synthetic_trace(
    trace_id="trc-fictional-review",
    episode_id="epi-uk-02",
    actions=[{"tool": "calculate", "result_minor": 1240}],
    outputs=[{"unresolved_minor": 1240}],
    human_effort=[HumanEffort(task_ref="tsk-fictional")],
)
print(trace.model_dump_json(indent=2))
```

An unmeasured effort value is `null`, not zero. A measured zero requires an explicit
`observed` or `self_reported` measurement method. Counts, review minutes and rework
minutes are separate fields. This alpha does not infer margin improvements or include
effort in the ranking formula.

After a normal benchmark run, export the verified Actions with:

```sh
uv run python scripts/export_trace.py results/example/run.json --output results/example/trace.json
```

Use the actual run artifact path printed by the CLI. The exporter checks the persisted
artifact first and sanitizes the resulting Actions. It cannot certify that arbitrary
author-supplied fixture content is safe for publication: authors must use fictional data.

The `world_or_prod` discriminator supports interchange with private systems; it is not
a public production connector. Production capture, practitioner identity, clustering,
post-training, generated private evals and deployment decisions remain applied-only.
The documented flow is trace → correction → adjudicated difference → cluster → bounded
improvement → targeted eval → regression suite → deployment decision. Only the schemas,
validators and synthetic export are implemented here.
