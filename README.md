# Mirror Firm

Mirror Firm is an open-source synthetic accounting firm and agent-evaluation
environment. It measures safe, stateful accounting-practice behaviour over time;
it is not an accounting system and must not be connected to real client data.

This repository currently contains the WP-01 project scaffold. Domain models, worlds,
tools, episodes, and evaluation logic are intentionally deferred to their designated
work packets in `SPEC.md`.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
make setup
make fmt lint type test
```

## Attribution

Mirror Firm adapts the provider-adapter, agent-loop, and report-shell patterns from
[Harvey LAB](https://github.com/harveyai/harvey-labs), released under the MIT licence,
at commit `55510f0e609ffa5cf6f5df17d9a813ce4bb33d0c`. See `NOTICE` and
`LICENSES/harvey-labs.MIT.txt`. No Harvey LAB legal tasks, task schemas, skills, tools,
or legal-domain content are included.

## Licence

Mirror Firm code is MIT-licensed. Original synthetic worlds and documents will be
licensed CC BY 4.0 when introduced in later work packets.
