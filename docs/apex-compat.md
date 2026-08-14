# APEX-Accounting compatibility

Mirror Firm can install the public APEX-Accounting development set as an external,
read-only pack:

```sh
mirror-firm packs install apex-accounting --revision <40-or-64-character-hf-revision>
```

The importer downloads `mercor/apex-accounting` into
`~/.cache/mirror-firm/packs/apex-accounting/<revision>`. It requires a pinned immutable
Hugging Face revision, records that revision plus SHA-256 hashes for every downloaded
source file in `pack-manifest.json`, and writes `apex-accounting.lock.json` beside the
revision cache. A cached pack with an absent, contradictory, or unpinned lock is refused.

APEX-Accounting is attributed to Mercor under CC BY 4.0; see the associated paper,
[arXiv:2607.27189](https://arxiv.org/abs/2607.27189). It is downloaded on demand and no
APEX task, world file, task file, or gold answer is vendored in this repository.

Each `data/dev.jsonl` source row becomes a `static_task`, not a Mirror Firm accounting
world. The public download stores shared assets under `world/` and task-specific assets
under `task_files/`; these source paths are resolved through each row's
`context_files` list. The importer records their source path, origin, and hash in the
installed manifest, then mounts only the selected files at the agent-facing flat,
read-only runtime root. This is the `filesystem/contents` + task-file convention in
SPEC §J: it describes the runtime mount, not a required upstream download layout.
The static runner only exposes `read_document`,
`read_table`, `search_documents`, `aggregate_table`, `calculate`,
`compare_datasets`, and `finish_episode`, with a 500-step / 5,000,000-token budget and
a console deliverable. QuickBooks behaviour and Mercor's unreleased official grading
template are not implemented; the static task uses the published rubric as binary judge
criteria instead. Results carry the source revision and this label:

> APEX-Accounting public dev set via Mirror Firm importer - external; not comparable to
> the official leaderboard

The public development data includes reference answers. The installed pack is therefore
marked `contamination: public-reference-answers` and is structurally rejected from
Mirror Firm headline-suite aggregation. Gold outputs are not written into static episode
documents or agent context. They are available only through the explicit inspection
command:

```sh
mirror-firm packs show-gold apex-accounting <task-id> --revision <revision>
```

The importer rejects unpinned, malformed, incomplete, duplicate, dangling,
path-traversing, stateful, non-fictional, secret-bearing, or hash-mismatched source
data before installation. This permits development-pack experimentation without
weakening the UK/US world compiler, tool registry, ownership, or evaluation contracts.
