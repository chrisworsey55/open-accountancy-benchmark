"""Regression coverage for the published APEX development-source layout.

The fixture deliberately contains only invented structural data.  It mirrors the
public layout and field names without copying an upstream prompt, task name, or answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from mirrorfirm.evaluation.qualitative.judge import _render_prompt
from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)
from mirrorfirm.packs.apex_accounting import (
    APEX_ACCOUNTING_REPOSITORY,
    ApexImportError,
    StaticTaskRunner,
    install_apex_accounting,
    load_installed_pack,
    load_static_task,
)
from mirrorfirm.packs.apex_accounting.importer import _safe_relative_path

REVISION = "b" * 40
REAL_PIN = "bf5e8c99117b7ee763d79ad2c64563ac844d77d2"


class _FinishOnlyAdapter(ModelAdapter):
    """A local deterministic adapter that records the static runner boundary."""

    def __init__(self) -> None:
        super().__init__("apex-fixture-script")
        self.tools: list[ProviderPayload] = []
        self.messages: list[ProviderPayload] = []

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        self.messages = messages
        self.tools = tools
        return ModelResponse(
            message={"role": "assistant", "content": "fictional final answer"},
            tool_calls=[
                ToolCall(
                    id="finish",
                    name="finish_episode",
                    arguments=json.dumps(
                        {
                            "summary": "Completed the fictional static task.",
                            "deliverable": "Fictional console deliverable.",
                        }
                    ),
                )
            ],
            input_tokens=1,
            output_tokens=1,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [{"role": "tool", "results": results}]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}


class _ReadThenFinishAdapter(_FinishOnlyAdapter):
    """Read one agent-visible asset before producing a fictional deliverable."""

    def __init__(self, asset_path: str) -> None:
        super().__init__()
        self._asset_path = asset_path
        self._call_count = 0

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        self.messages = messages
        self.tools = tools
        if self._call_count == 0:
            self._call_count += 1
            return ModelResponse(
                message={"role": "assistant", "content": "read fictional evidence"},
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="read_document",
                        arguments=json.dumps({"doc_id": self._asset_path}),
                    )
                ],
                input_tokens=1,
                output_tokens=1,
            )
        return super().chat(messages, tools)


class _ContextSelectingReadThenFinishAdapter(_FinishOnlyAdapter):
    """Select an evidence filename exclusively from the first provider message."""

    def __init__(self) -> None:
        super().__init__()
        self._call_count = 0
        self.selected_filename: str | None = None

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        self.messages = messages
        self.tools = tools
        if self._call_count == 0:
            self._call_count += 1
            user_prompt = messages[1].get("content")
            assert isinstance(user_prompt, str)
            _, context_json = user_prompt.split("\n", maxsplit=1)
            context = json.loads(context_json)
            inventory = context["authorised_runtime_files"]
            assert isinstance(inventory, list)
            selected = next(
                filename
                for filename in inventory
                if isinstance(filename, str) and filename.endswith(".txt")
            )
            self.selected_filename = selected
            return ModelResponse(
                message={"role": "assistant", "content": "read listed evidence"},
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="read_document",
                        arguments=json.dumps({"doc_id": selected}),
                    )
                ],
                input_tokens=1,
                output_tokens=1,
            )
        return super().chat(messages, tools)


class _PassingJudgeAdapter(ModelAdapter):
    """Separate scripted qualitative judge that records every imported criterion call."""

    def __init__(self) -> None:
        super().__init__("apex-scripted-judge")
        self.messages: list[list[ProviderPayload]] = []

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        assert tools == []
        self.messages.append(messages)
        return ModelResponse(
            message={
                "role": "assistant",
                "content": json.dumps(
                    {"verdict": "pass", "reasoning": "scripted fictional pass"}
                ),
            },
            input_tokens=1,
            output_tokens=1,
        )

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [{"role": "tool", "results": results}]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}


def _copy_downloader(source: Path) -> Callable[[str, str, Path], None]:
    def download(repository: str, revision: str, destination: Path) -> None:
        assert repository == APEX_ACCOUNTING_REPOSITORY
        assert revision == REVISION
        shutil.copytree(source, destination, dirs_exist_ok=True)

    return download


@pytest.fixture
def real_layout_source(tmp_path: Path) -> Path:
    """Create the 10/90/16 public-source shape using solely fictional values."""

    root = tmp_path / "source"
    data = root / "data"
    world = root / "world"
    task_files = root / "task_files"
    data.mkdir(parents=True)
    world.mkdir()
    task_files.mkdir()

    rows: list[dict[str, object]] = []
    for task_number in range(10):
        task_id = f"fixture-task-{task_number + 1:02d}"
        context_files: list[str] = []
        for file_number in range(9):
            name = f"world-{task_number + 1:02d}-source-{file_number + 1:02d}.txt"
            relative = f"world/world-{task_number + 1:02d}/{name}"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"Fictional shared accounting evidence {task_number}:{file_number}.",
                encoding="utf-8",
            )
            context_files.append(name)
        if task_number < 8:
            note_name = f"task-{task_number + 1:02d}-note.txt"
            relative = f"task_files/task-{task_number + 1:02d}/{note_name}"
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"Fictional task-specific accounting note {task_number}.",
                encoding="utf-8",
            )
            context_files.append(target.name)
            if task_number < 8:
                supplement_name = f"task-{task_number + 1:02d}-supplement.txt"
                second = f"task_files/task-{task_number + 1:02d}/{supplement_name}"
                second_target = root / second
                second_target.write_text(
                    f"Fictional accounting supplement {task_number}.", encoding="utf-8"
                )
                context_files.append(second_target.name)
        rows.append(
            {
                "task_id": task_id,
                "task_name": f"fictional-source-task-{task_number + 1:02d}",
                "world_id": f"fictional-world-{task_number + 1:02d}",
                "prompt": f"Complete fictional accounting task {task_number + 1}.",
                "context_files": context_files,
                "rubric": [
                    {
                        "id": "criterion-1",
                        "criterion_type": "binary",
                        "description": "Produces a fictional console deliverable.",
                    }
                ],
                "gold_output": f"FIXTURE GOLD {task_number + 1}",
                "metadata": {
                    "category": "fictional",
                    "output_type": "console",
                },
            }
        )
    # Eight task-specific directories with two files each: exactly sixteen files.
    assert sum(len(row["context_files"]) for row in rows) == 106
    (data / "dev.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return root


def _install(source: Path, tmp_path: Path) -> Path:
    return install_apex_accounting(
        revision=REVISION,
        cache_root=tmp_path / "cache",
        downloader=_copy_downloader(source),
    ).root


def _real_source() -> Path:
    source = os.environ.get("MIRRORFIRM_APEX_REAL_SOURCE")
    if source is None:
        pytest.skip("set MIRRORFIRM_APEX_REAL_SOURCE to a pinned temporary download")
    return Path(source)


def _install_real_source(source: Path, tmp_path: Path) -> Path:
    def downloader(repository: str, revision: str, destination: Path) -> None:
        assert repository == APEX_ACCOUNTING_REPOSITORY
        assert revision == REAL_PIN
        shutil.copytree(source, destination, dirs_exist_ok=True)

    return install_apex_accounting(
        revision=REAL_PIN, cache_root=tmp_path / "cache", downloader=downloader
    ).root


def test_importer_accepts_public_dev_jsonl_world_and_task_files_layout(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """The actual source shape needs no fabricated metadata or tasks JSON files."""

    root = _install(real_layout_source, tmp_path)

    pack = load_installed_pack(root)
    task = load_static_task(root, "fixture-task-01")
    assert (root / "source" / "data" / "dev.jsonl").is_file()
    assert pack.task_count == 10
    assert pack.world_file_count == 90
    assert pack.task_file_count == 16
    assert task.instruction == "Complete fictional accounting task 1."
    assert task.source_task_name == "fictional-source-task-01"
    assert (
        task.qualitative_criteria[0].prompt
        == "Produces a fictional console deliverable."
    )
    assert "FIXTURE GOLD" not in task.model_dump_json()
    assert "FIXTURE GOLD" not in json.dumps(
        task.model_visible_context(), sort_keys=True
    )
    assert {asset.origin for asset in task.runtime_files} == {"world", "task_files"}
    assert all("/" not in asset.runtime_path for asset in task.runtime_files)
    assert all(
        (root / "runtime" / task.task_id / asset.runtime_path).stat().st_mode
        & stat.S_IWUSR
        == 0
        for asset in task.runtime_files
    )
    assert all(
        mapping.sha256
        == hashlib.sha256(
            (root / "runtime" / mapping.task_id / mapping.runtime_path).read_bytes()
        ).hexdigest()
        for mapping in pack.manifest.task_asset_mappings
    )


def test_flat_runtime_exposes_only_files_selected_by_context_files(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """A task cannot access an otherwise valid source file it was not assigned."""

    root = _install(real_layout_source, tmp_path)
    task = load_static_task(root, "fixture-task-01")
    unassigned = "world-10-source-01.txt"
    assert unassigned not in {asset.runtime_path for asset in task.runtime_files}
    assert not (root / "runtime" / task.task_id / unassigned).exists()


def test_pack_reopens_offline_and_detects_source_or_runtime_tampering(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """Both the retained source and its flat runtime copies are hash-verified."""

    root = _install(real_layout_source, tmp_path)
    assert load_installed_pack(root).revision == REVISION
    task = load_static_task(root, "fixture-task-01")
    runtime_path = root / "runtime" / task.task_id / task.runtime_files[0].runtime_path
    runtime_path.chmod(0o644)
    runtime_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ApexImportError, match="runtime asset hash"):
        load_installed_pack(root)


def test_gold_is_never_written_to_public_episode_runtime_or_report_artifacts(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """The fixture canary remains solely inside source/dev.jsonl and explicit inspection."""

    root = _install(real_layout_source, tmp_path)
    public_files = [
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_relative_to(root / "source")
    ]
    assert all(
        "FIXTURE GOLD" not in path.read_bytes().decode("utf-8", errors="ignore")
        for path in public_files
    )


def test_static_runner_captures_a_console_deliverable_from_flat_runtime(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """A static task finishes without world access and preserves its deliverable."""

    root = _install(real_layout_source, tmp_path)
    adapter = _FinishOnlyAdapter()
    result = StaticTaskRunner(root, load_static_task(root, "fixture-task-01")).run(
        adapter
    )

    assert result.completed is True
    assert result.agent_result["finish_summary"] == {
        "summary": "Completed the fictional static task.",
        "deliverable": "Fictional console deliverable.",
        "deliverable_refs": [],
        "unresolved_items": [],
        "task_id": "fixture-task-01",
    }
    assert {tool["name"] for tool in adapter.tools} == {
        "read_document",
        "read_table",
        "search_documents",
        "aggregate_table",
        "calculate",
        "compare_datasets",
        "finish_episode",
    }


@pytest.mark.parametrize(
    "path",
    [
        "..\\escape",
        "dir\\..\\escape",
        "C:\\escape",
        "\\\\server\\share",
        "world/mixed\\escape",
        "world//repeated.txt",
        "%2e%2e/escape",
        "unicode\u29f8slash.txt",
        "world/\u2215slash.txt",
        "world/\u2044slash.txt",
        "world/\uff0fslash.txt",
        "world/\uff3cslash.txt",
        "world/\u29f5reverse.txt",
        "world/\u29f9reverse.txt",
        "world/\ufe68reverse.txt",
        "world/\u0337overlay.txt",
        "world/e\u0301.txt",
        "world/\uff21.txt",
    ],
)
def test_safe_relative_path_rejects_cross_platform_or_ambiguous_paths(
    path: str,
) -> None:
    """Source and runtime paths have one canonical non-ambiguous representation."""

    with pytest.raises(ApexImportError, match="safe relative"):
        _safe_relative_path(path, "fixture path")


def test_safe_relative_path_retains_unambiguous_ordinary_unicode() -> None:
    """Unicode names that are not separator-like or normalization-ambiguous remain safe."""

    assert (
        _safe_relative_path("world/caf\u00e9.txt", "fixture path")
        == "world/caf\u00e9.txt"
    )


def test_importer_rejects_duplicate_flat_runtime_names(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """Two selected source files cannot collide once mounted at the flat root."""

    rows_path = real_layout_source / "data" / "dev.jsonl"
    rows = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
    ]
    original = real_layout_source / "world/world-01/world-01-source-02.txt"
    extra = "world/other/world-01-source-01.txt"
    target = real_layout_source / extra
    target.parent.mkdir(parents=True)
    original.replace(target)
    rows[0]["context_files"][0] = "world/world-01/world-01-source-01.txt"
    rows[0]["context_files"][1] = extra
    rows_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ApexImportError, match="flat-root"):
        _install(real_layout_source, tmp_path)


@pytest.mark.parametrize(
    "hostile_name",
    [
        "unicode\u29f8slash.txt",
        "unicode\u2215slash.txt",
        "unicode\u2044slash.txt",
        "unicode\uff0fslash.txt",
        "unicode\uff3cslash.txt",
        "unicode\u29f5reverse.txt",
        "unicode\u29f9reverse.txt",
        "unicode\ufe68reverse.txt",
        "unicode\uff21.txt",
    ],
)
def test_importer_rejects_unicode_separator_or_normalization_attack_before_materialisation(
    real_layout_source: Path, tmp_path: Path, hostile_name: str
) -> None:
    """Unsafe Unicode names never reach installation, flattening, or runtime copies."""

    rows_path = real_layout_source / "data" / "dev.jsonl"
    rows = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
    ]
    original = real_layout_source / "world/world-01/world-01-source-01.txt"
    hostile = original.with_name(hostile_name)
    original.replace(hostile)
    rows[0]["context_files"][0] = hostile_name
    rows_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ApexImportError, match="safe relative"):
        _install(real_layout_source, tmp_path)


def test_importer_materialises_unambiguous_ordinary_unicode_filename(
    real_layout_source: Path, tmp_path: Path
) -> None:
    """Fail-closed separator checks do not prohibit normal, canonical Unicode names."""

    rows_path = real_layout_source / "data" / "dev.jsonl"
    rows = [
        json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()
    ]
    original = real_layout_source / "world/world-01/world-01-source-01.txt"
    safe_name = "caf\u00e9.txt"
    original.replace(original.with_name(safe_name))
    rows[0]["context_files"][0] = safe_name
    rows_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )

    root = _install(real_layout_source, tmp_path)
    task = load_static_task(root, "fixture-task-01")
    assert safe_name in {asset.runtime_path for asset in task.runtime_files}
    assert (root / "runtime" / task.task_id / safe_name).is_file()


def test_real_pinned_source_layout_when_available() -> None:
    """Exercise the actual public layout without retaining or printing task content."""

    source = os.environ.get("MIRRORFIRM_APEX_REAL_SOURCE")
    if source is None:
        pytest.skip("set MIRRORFIRM_APEX_REAL_SOURCE to a pinned temporary download")
    root = Path(source)
    rows_path = root / "data" / "dev.jsonl"
    rows = [
        json.loads(line)
        for line in rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 10
    assert all(isinstance(row.get("task_name"), str) for row in rows)
    assert all(isinstance(row.get("context_files"), list) for row in rows)
    assert all(
        isinstance(criterion.get("description"), str)
        for row in rows
        for criterion in row.get("rubric", [])
        if isinstance(criterion, dict)
    )
    assert len([path for path in (root / "world").rglob("*") if path.is_file()]) == 90
    assert (
        len([path for path in (root / "task_files").rglob("*") if path.is_file()]) == 16
    )
    # A digest proves the gold field is present without emitting its content.
    assert all(
        len(hashlib.sha256(str(row["gold_output"]).encode()).hexdigest()) == 64
        for row in rows
    )
    assert REAL_PIN == "bf5e8c99117b7ee763d79ad2c64563ac844d77d2"


def test_real_task_evidence_inventory_reaches_provider_and_transcript(
    tmp_path: Path,
) -> None:
    """Every real task asset must be visible before the provider's first call."""

    root = _install_real_source(_real_source(), tmp_path)
    task = load_static_task(root, sorted((root / "tasks").glob("*.json"))[0].stem)
    adapter = _FinishOnlyAdapter()
    transcript = tmp_path / "static-transcript.jsonl"

    StaticTaskRunner(root, task).run(adapter, transcript_path=str(transcript))

    initial_messages = json.dumps(adapter.messages[:2], ensure_ascii=False)
    assert all(asset.runtime_path in initial_messages for asset in task.runtime_files)
    assert all(
        asset.runtime_path in transcript.read_text(encoding="utf-8")
        for asset in task.runtime_files
    )


def test_all_real_tasks_expose_only_their_authorised_evidence_inventory(
    tmp_path: Path,
) -> None:
    """All ten actual tasks disclose every allowed filename, and nothing outside scope."""

    root = _install_real_source(_real_source(), tmp_path)
    tasks = [
        load_static_task(root, path.stem)
        for path in sorted((root / "tasks").glob("*.json"))
    ]
    all_filenames = {
        asset.runtime_path for task in tasks for asset in task.runtime_files
    }
    source_rows = [
        json.loads(line)
        for line in (_real_source() / "data" / "dev.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    gold_values = {str(row["gold_output"]) for row in source_rows}
    for task in tasks:
        context = task.model_visible_context()
        inventory = context["authorised_runtime_files"]
        assert inventory == [asset.runtime_path for asset in task.runtime_files]
        assert all(isinstance(filename, str) for filename in inventory)
        assert set(inventory) == {asset.runtime_path for asset in task.runtime_files}
        unassigned = all_filenames.difference(set(inventory))
        context_surface = json.dumps(context, ensure_ascii=False, sort_keys=True)
        assert all(filename not in context_surface for filename in unassigned)
        assert "source_metadata" not in context_surface
        assert "source_path" not in context_surface
        assert "sha256" not in context_surface
        assert all(gold not in context_surface for gold in gold_values)
        adapter = _FinishOnlyAdapter()
        StaticTaskRunner(root, task).run(adapter)
        provider_surface = json.dumps(adapter.messages[:2], ensure_ascii=False)
        assert all(filename in provider_surface for filename in inventory)


def test_real_static_runner_reads_a_filename_selected_only_from_provider_context(
    tmp_path: Path,
) -> None:
    """A deterministic provider can discover and read real evidence without a list tool."""

    root = _install_real_source(_real_source(), tmp_path)
    tasks = [
        load_static_task(root, path.stem)
        for path in sorted((root / "tasks").glob("*.json"))
    ]
    task = next(
        item
        for item in tasks
        if any(asset.runtime_path.endswith(".txt") for asset in item.runtime_files)
    )
    adapter = _ContextSelectingReadThenFinishAdapter()
    transcript = tmp_path / "context-selected-static-run.jsonl"

    result = StaticTaskRunner(root, task).run(adapter, transcript_path=str(transcript))

    assert result.completed is True
    assert adapter.selected_filename in {
        asset.runtime_path for asset in task.runtime_files
    }
    assert adapter.selected_filename in transcript.read_text(encoding="utf-8")


def test_all_real_imported_criteria_enter_separate_static_judge_path(
    tmp_path: Path,
) -> None:
    """The 89 actual public rubrics are all evaluated without exposing gold."""

    root = _install_real_source(_real_source(), tmp_path)
    tasks = [
        load_static_task(root, path.stem)
        for path in sorted((root / "tasks").glob("*.json"))
    ]
    judge = _PassingJudgeAdapter()
    results = [
        StaticTaskRunner(root, task).run(
            _FinishOnlyAdapter(),
            judge_adapters={"scripted-judge": judge},
            judge_models=["scripted-judge"],
        )
        for task in tasks
    ]

    assert sum(len(result.evaluation.criterion_results) for result in results) == 89
    assert all(result.evaluation.passed for result in results)
    assert len(judge.messages) == 89
    gold_values = [
        str(row["gold_output"])
        for row in (
            json.loads(line)
            for line in (_real_source() / "data" / "dev.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        )
    ]
    assert all(
        all(
            gold not in json.dumps(messages, ensure_ascii=False) for gold in gold_values
        )
        for messages in judge.messages
    )


def test_real_pinned_fresh_installations_are_byte_deterministic(tmp_path: Path) -> None:
    """The actual pinned source produces identical public installed artifacts twice."""

    first = _install_real_source(_real_source(), tmp_path / "first")
    second = _install_real_source(_real_source(), tmp_path / "second")

    assert (first / "pack-manifest.json").read_bytes() == (
        second / "pack-manifest.json"
    ).read_bytes()
    assert (first.parent / "apex-accounting.lock.json").read_bytes() == (
        second.parent / "apex-accounting.lock.json"
    ).read_bytes()
    assert {
        path.name: path.read_bytes()
        for path in sorted((first / "tasks").glob("*.json"))
    } == {
        path.name: path.read_bytes()
        for path in sorted((second / "tasks").glob("*.json"))
    }
    assert load_installed_pack(first).manifest.task_asset_mappings == (
        load_installed_pack(second).manifest.task_asset_mappings
    )


def test_real_pinned_install_and_static_run_when_available(tmp_path: Path) -> None:
    """Validate a local temporary copy of the real pin without emitting its contents."""

    source = os.environ.get("MIRRORFIRM_APEX_REAL_SOURCE")
    if source is None:
        pytest.skip("set MIRRORFIRM_APEX_REAL_SOURCE to a pinned temporary download")
    real_source = Path(source)

    def downloader(repository: str, revision: str, destination: Path) -> None:
        assert repository == APEX_ACCOUNTING_REPOSITORY
        assert revision == REAL_PIN
        shutil.copytree(real_source, destination, dirs_exist_ok=True)

    root = install_apex_accounting(
        revision=REAL_PIN, cache_root=tmp_path / "cache", downloader=downloader
    ).root
    pack = load_installed_pack(root)
    tasks = [
        load_static_task(root, path.stem)
        for path in sorted((root / "tasks").glob("*.json"))
    ]
    assert (pack.task_count, pack.world_file_count, pack.task_file_count) == (
        10,
        90,
        16,
    )
    assert len(pack.manifest.task_artifacts) == 10
    assert {artifact.relative_path for artifact in pack.manifest.task_artifacts} == {
        f"tasks/{task.task_id}.json" for task in tasks
    }
    assert all(task.runtime_files for task in tasks)
    assert all(
        "gold_output" not in path.read_text(encoding="utf-8")
        for path in (root / "tasks").glob("*.json")
    )
    task_with_text = next(
        task
        for task in tasks
        if any(asset.runtime_path.endswith(".txt") for asset in task.runtime_files)
    )
    text_asset = next(
        asset.runtime_path
        for asset in task_with_text.runtime_files
        if asset.runtime_path.endswith(".txt")
    )
    adapter = _ReadThenFinishAdapter(text_asset)
    transcript = tmp_path / "real-static-transcript.jsonl"
    result = StaticTaskRunner(root, task_with_text).run(
        adapter, transcript_path=str(transcript)
    )
    assert result.completed is True
    assert result.agent_result["finish_summary"] is not None
    source_rows = [
        json.loads(line)
        for line in (real_source / "data" / "dev.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    source_criterion_counts = {
        str(row["task_id"]): len(row["rubric"]) for row in source_rows
    }
    assert pack.manifest.criterion_count == sum(source_criterion_counts.values())
    assert {
        task.task_id: len(task.qualitative_criteria) for task in tasks
    } == source_criterion_counts
    gold_values = [
        str(row["gold_output"])
        for row in source_rows
        if isinstance(row.get("gold_output"), str)
    ]
    mounted_contents = "\n".join(
        path.read_bytes().decode("utf-8", errors="ignore")
        for path in (root / "runtime").rglob("*")
        if path.is_file()
    )
    judge_prompt_surface = "\n".join(
        _render_prompt(criterion, ["fictional static deliverable"])
        for task in tasks
        for criterion in task.qualitative_criteria
    )
    model_or_report_surface = json.dumps(
        {
            "manifest": pack.manifest.model_dump(mode="json"),
            "episode": tasks[0].model_dump(mode="json"),
            "context": tasks[0].model_visible_context(),
            "tools": adapter.tools,
            "messages": adapter.messages,
            "runner_result": result.agent_result,
            "transcript": transcript.read_text(encoding="utf-8"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert all(gold not in model_or_report_surface for gold in gold_values)
    assert all(gold not in mounted_contents for gold in gold_values)
    assert all(gold not in judge_prompt_surface for gold in gold_values)
