# Running models and five-run baselines

## One live run

Configure a supported provider credential in the shell, never in source, config, or
result files. Model IDs use provider prefixes: `openai/`, `anthropic/`, `google/`,
`mistral/`, or `fireworks/`.

```sh
export OPENAI_API_KEY='your-shell-secret'
mirror-firm run \
  --episode epi-uk-01 \
  --model openai/<model-name> \
  --judge-model openai/<judge-model-name> \
  --results-root results/my-run \
  --json
```

The CLI preflights the world and required qualitative judge configuration. It fails
without invoking a provider if credentials, output containment, validation, or run
budget requirements are unmet.

## Declarative sweeps

Use JSON or YAML:

```yaml
episodes: [epi-uk-01]
models: [openai/<model-name>]
runs: 5
judge_models: [openai/<judge-model-name>]
output_dir: results/openai-uk01-baseline
seed: 7
baseline: true
```

```sh
mirror-firm sweep sweep.yaml --json
```

The complete matrix is preflighted before the first provider call. It validates episode
and world pins, world gates, model/provider IDs, credentials, judge configuration,
positive run count, safe unused output paths, and contamination separation. A baseline
is exactly five fresh native runs. One model failure is recorded honestly and does not
turn into a completed zero-score run.

## Reference controls

The offline control is always:

> reference (scripted) - not model performance

It can validate the environment but cannot be a five-run model baseline, a native
comparison row, or a headline performance claim. Run it with `make demo` or:

```sh
mirror-firm run --episode epi-uk-01 --model reference-scripted --results-root /tmp/mf-ref
```

The repository intentionally publishes no real model baseline yet. See
[evaluation](evaluation.md) for `Pass@k` and `Pass^k` meaning.
