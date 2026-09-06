"""Pinned, validated, download-on-demand importer for APEX-Accounting.

The public APEX development pack is intentionally kept outside the repository.  The
importer materializes a content-addressed local cache of its source files and emits
only scrubbed static-task episode documents; expert gold answers are never copied into
those agent-visible documents.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal, TypeAlias, cast
from urllib.parse import unquote

from pydantic import JsonValue, ValidationError

from mirrorfirm.core.digest import canonical_json
from mirrorfirm.core.models import JudgeCriterion

from .models import (
    ApexPackLock,
    ApexPackManifest,
    GeneratedTaskArtifact,
    InstalledApexPack,
    RuntimeFile,
    SourceFile,
    StaticTaskBudget,
    StaticTaskEpisode,
    TaskAssetMapping,
)

APEX_ACCOUNTING_REPOSITORY: Final = "mercor/apex-accounting"
APEX_ACCOUNTING_PACK_ID: Final = "apex-accounting"
APEX_ACCOUNTING_LABEL: Final = (
    "APEX-Accounting public dev set via Franklin & McGrath importer - external; "
    "not comparable to the official leaderboard"
)
APEX_ALLOWED_TOOLS: Final[tuple[str, ...]] = (
    "read_document",
    "read_table",
    "search_documents",
    "aggregate_table",
    "calculate",
    "compare_datasets",
    "finish_episode",
)
EXPECTED_TASK_COUNT: Final = 10
EXPECTED_WORLD_FILE_COUNT: Final = 90
EXPECTED_TASK_FILE_COUNT: Final = 16

DownloadFunction: TypeAlias = Callable[[str, str, Path], None]
JsonMapping: TypeAlias = Mapping[str, object]
AssetOrigin: TypeAlias = Literal["world", "task_files"]

_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TASK_FILENAMES: Final[tuple[str, ...]] = (
    "tasks_and_rubrics.json",
    "tasks.json",
)
_WORLD_ARCHIVES: Final[tuple[str, ...]] = (
    "world_files.zip",
    "world_files_zipped",
    "world-files.zip",
)
_TASK_ARCHIVES: Final[tuple[str, ...]] = ("task_files.zip", "task-files.zip")
_REAL_TASK_DATA: Final = "data/dev.jsonl"
_REAL_WORLD_ROOT: Final = "world"
_REAL_TASK_ROOT: Final = "task_files"
_LEGACY_WORLD_ROOT: Final = "filesystem/contents"
_HF_LOCAL_METADATA_ROOT: Final = ".cache"
_FORBIDDEN_STATEFUL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "client",
        "client_id",
        "clients",
        "engagement",
        "engagement_id",
        "engagements",
        "records",
        "world_records",
        "events",
        "actions",
        "provenance",
        "journal",
        "journals",
        "bank_transactions",
    }
)


class ApexImportError(ValueError):
    """An APEX source, pinned cache, or static task failed strict validation."""


class PackContaminationError(ApexImportError):
    """A public-answer pack was offered to a headline Franklin & McGrath suite."""


@dataclass
class _SourceAsset:
    """A source asset resolved from a task's public context declaration."""

    source_path: str
    runtime_path: str
    origin: AssetOrigin


@dataclass
class _RawTask:
    """Internal parsed task metadata, retaining gold only at the importer boundary."""

    task_id: str
    source_task_name: str
    world_id: str
    prompt: str
    criteria: tuple[JudgeCriterion, ...]
    gold_output: str | None
    source_metadata: JsonMapping
    world_files: tuple[str, ...] = ()
    task_files: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    assets: tuple[_SourceAsset, ...] = ()


def default_cache_root() -> Path:
    """Return the SPEC §J download cache, without relying on a shell expansion."""

    return Path.home() / ".cache" / "mirror-firm" / "packs"


def installed_pack_path(revision: str, *, cache_root: str | Path | None = None) -> Path:
    """Resolve a verified revision's canonical cache directory."""

    _require_pinned_revision(revision)
    root = Path(cache_root) if cache_root is not None else default_cache_root()
    return root / APEX_ACCOUNTING_PACK_ID / revision


def install_apex_accounting(
    *,
    revision: str,
    cache_root: str | Path | None = None,
    downloader: DownloadFunction | None = None,
) -> InstalledApexPack:
    """Download one pinned revision, validate it, and atomically install the pack.

    ``downloader`` is a narrow test seam.  Production uses the Hugging Face ``hf``
    client exactly as specified, with the dataset repository type made explicit.
    """

    _require_pinned_revision(revision)
    destination = installed_pack_path(revision, cache_root=cache_root)
    if destination.exists():
        return load_installed_pack(destination)

    pack_root = destination.parent
    pack_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{revision}.", dir=pack_root))
    try:
        source_root = temporary / "source"
        active_downloader = downloader or _download_with_hf
        active_downloader(APEX_ACCOUNTING_REPOSITORY, revision, source_root)
        if not source_root.is_dir():
            raise ApexImportError("downloader did not create a source directory")
        _remove_downloader_metadata(source_root)
        _expand_known_archives(source_root)
        raw_tasks = _validate_source(source_root)
        _self_test_public_gold_no_leak(raw_tasks)
        _write_static_tasks(temporary, source_root, raw_tasks, revision)
        manifest = _write_manifest(temporary, source_root, raw_tasks, revision)
        _write_json(temporary / "pack-manifest.json", manifest.model_dump(mode="json"))
        _self_test_public_gold_no_leak_in_artifacts(temporary, raw_tasks)
        if destination.exists():
            raise ApexImportError(
                f"pinned cache destination already exists: {destination}"
            )
        os.replace(temporary, destination)
        _write_lock(destination, manifest)
        return load_installed_pack(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def load_installed_pack(root: str | Path) -> InstalledApexPack:
    """Load and cryptographically verify a previously installed pinned pack."""

    directory = Path(root)
    manifest_path = directory / "pack-manifest.json"
    manifest_bytes = _read_verified_regular_file(
        manifest_path, "installed pack manifest"
    )
    try:
        manifest = ApexPackManifest.model_validate_json(manifest_bytes)
    except (ValidationError, json.JSONDecodeError) as error:
        raise ApexImportError(
            "installed pack manifest is missing or invalid"
        ) from error
    _require_pinned_revision(manifest.revision)
    expected_directory = directory.parent / manifest.revision
    if directory.resolve() != expected_directory.resolve():
        raise ApexImportError(
            "installed pack location does not match its pinned revision"
        )
    _verify_source_files(directory, manifest)
    _verify_lock(directory, manifest, manifest_bytes)
    verified_artifacts = _load_verified_task_artifacts(directory, manifest)
    tasks = [task for _, _, task in verified_artifacts]
    _validate_runtime_task_roots(directory / "runtime", tasks)
    verified_runtime_bytes: dict[str, dict[str, bytes]] = {}
    for _, _, task in verified_artifacts:
        runtime_bytes = _read_verified_runtime_assets(directory, task)
        _validate_static_task_assets(task, runtime_bytes)
        verified_runtime_bytes[task.task_id] = runtime_bytes
    _validate_manifest_asset_mappings(manifest, tasks)
    return InstalledApexPack(
        directory,
        manifest,
        verified_tasks={task.task_id: task for _, _, task in verified_artifacts},
        verified_task_bytes={
            task.task_id: data for _, data, task in verified_artifacts
        },
        verified_runtime_bytes=verified_runtime_bytes,
    )


def load_static_task(root: str | Path, task_id: str) -> StaticTaskEpisode:
    """Load a verified agent-visible static task without reading its gold answer."""

    pack = load_installed_pack(root)
    _require_safe_identifier(task_id, "task id")
    try:
        task, _, _ = pack.verified_task(task_id)
    except KeyError as error:
        raise ApexImportError(
            f"static task {task_id!r} is missing or invalid"
        ) from error
    return task


def show_gold_output(root: str | Path, task_id: str) -> str:
    """Return a public dev answer only through this explicit inspection path."""

    pack = load_installed_pack(root)
    _require_safe_identifier(task_id, "task id")
    for raw in _load_raw_tasks(pack.root / "source"):
        if raw.task_id == task_id:
            if raw.gold_output is None:
                raise ApexImportError(f"task {task_id!r} has no public gold output")
            return raw.gold_output
    raise ApexImportError(f"task {task_id!r} is not present in the installed pack")


def _download_with_hf(repository: str, revision: str, destination: Path) -> None:
    """Invoke the documented ``hf download`` command without a shell boundary."""

    try:
        subprocess.run(
            [
                "hf",
                "download",
                repository,
                "--repo-type",
                "dataset",
                "--revision",
                revision,
                "--local-dir",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ApexImportError(
            "APEX-Accounting download failed; install the Hugging Face CLI and provide "
            "a reachable pinned revision"
        ) from error


def _require_pinned_revision(revision: str) -> None:
    if not _REVISION_PATTERN.fullmatch(revision):
        raise ApexImportError(
            "APEX-Accounting requires a pinned 40-64 character lowercase hex revision"
        )


def _require_safe_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise ApexImportError(f"{label} must be a safe stable identifier")
    return value


def _safe_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ApexImportError(f"{label} must be a non-empty safe relative path")
    if (
        "\\" in value
        or value.startswith("/")
        or value.startswith("//")
        or re.match(r"^[A-Za-z]:", value) is not None
        or "//" in value
        or unquote(value) != value
        or unicodedata.normalize("NFC", value) != value
        or unicodedata.normalize("NFKC", value) != value
        or _contains_separator_confusable(value)
    ):
        raise ApexImportError(f"{label} must be a safe relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or path.as_posix() != value
    ):
        raise ApexImportError(f"{label} must be a safe relative path")
    if any(not part for part in path.parts):
        raise ApexImportError(f"{label} must be a safe relative path")
    return path.as_posix()


def _contains_separator_confusable(value: str) -> bool:
    """Reject non-ASCII slash/backslash glyphs before they reach filesystem APIs."""

    for character in value:
        if ord(character) <= 0x7F:
            continue
        normalized = unicodedata.normalize("NFKC", character)
        name = unicodedata.name(character, "")
        if normalized in {"/", "\\"} or "SLASH" in name or "SOLIDUS" in name:
            return True
    return False


def _remove_downloader_metadata(source_root: Path) -> None:
    """Discard Hugging Face local bookkeeping before hashing imported source data."""

    metadata = source_root / _HF_LOCAL_METADATA_ROOT
    if metadata.exists():
        if metadata.is_symlink() or not metadata.is_dir():
            raise ApexImportError("APEX downloader metadata path is unsafe")
        shutil.rmtree(metadata)


def _expand_known_archives(source_root: Path) -> None:
    """Expand only the two documented archive families after safe-member checks."""

    if (
        (source_root / _REAL_TASK_DATA).is_file()
        and (source_root / _REAL_WORLD_ROOT).is_dir()
        and (source_root / _REAL_TASK_ROOT).is_dir()
    ):
        return

    world_root = source_root / _LEGACY_WORLD_ROOT
    if not world_root.is_dir():
        archive = _find_archive(source_root, _WORLD_ARCHIVES)
        if archive is None:
            raise ApexImportError(
                "APEX source is missing filesystem/contents world files"
            )
        _extract_safe_zip(
            archive,
            world_root,
            removable_prefixes=("filesystem/contents", "world_files"),
        )
    task_root = source_root / "task_files"
    if not task_root.is_dir():
        archive = _find_archive(source_root, _TASK_ARCHIVES)
        if archive is None:
            raise ApexImportError("APEX source is missing task_files")
        _extract_safe_zip(archive, task_root, removable_prefixes=("task_files",))


def _find_archive(root: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        path = root / name
        if path.is_file():
            return path
    return None


def _extract_safe_zip(
    archive_path: Path,
    destination: Path,
    *,
    removable_prefixes: tuple[str, ...],
) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = tuple(archive.infolist())
            relative_members: list[tuple[zipfile.ZipInfo, str]] = []
            for member in members:
                relative = _safe_relative_path(member.filename, "unsafe archive member")
                mode = member.external_attr >> 16
                if mode and (mode & 0o170000) == 0o120000:
                    raise ApexImportError("unsafe archive contains a symbolic link")
                relative_members.append((member, relative))
            normalised = _strip_archive_prefix(relative_members, removable_prefixes)
            seen: set[str] = set()
            for _, relative in normalised:
                if relative in seen:
                    raise ApexImportError(
                        "unsafe archive contains duplicate member paths"
                    )
                seen.add(relative)
            destination.mkdir(parents=True, exist_ok=True)
            for member, relative in normalised:
                if member.is_dir():
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as error:
        raise ApexImportError("APEX source has an invalid or unsafe archive") from error


def _strip_archive_prefix(
    members: Sequence[tuple[zipfile.ZipInfo, str]], prefixes: Sequence[str]
) -> tuple[tuple[zipfile.ZipInfo, str], ...]:
    """Accept the documented archive roots while retaining a single safe asset tree."""

    file_names = [relative for member, relative in members if not member.is_dir()]
    for prefix in prefixes:
        marker = f"{prefix}/"
        if file_names and all(name.startswith(marker) for name in file_names):
            stripped: list[tuple[zipfile.ZipInfo, str]] = []
            for member, relative in members:
                if relative == prefix:
                    continue
                if not relative.startswith(marker):
                    if member.is_dir() and prefix.startswith(f"{relative}/"):
                        continue
                    raise ApexImportError("archive root is contradictory")
                stripped.append(
                    (
                        member,
                        _safe_relative_path(
                            relative.removeprefix(marker), "archive member"
                        ),
                    )
                )
            return tuple(stripped)
    return tuple(members)


def _validate_source(source_root: Path) -> tuple[_RawTask, ...]:
    if (source_root / _REAL_TASK_DATA).is_file():
        return _validate_real_source(source_root)

    # Retain support for the original documented archive fixture only.  The public
    # dataset itself is always parsed through data/dev.jsonl above.
    metadata = _read_json_mapping(source_root / "metadata.json", "metadata")
    if metadata.get("synthetic") is not True:
        raise ApexImportError("APEX source must declare fictional synthetic data")
    if metadata.get("license") != "CC-BY-4.0":
        raise ApexImportError("APEX source must declare the CC-BY-4.0 licence")
    _reject_nonfictional_markers(metadata, "metadata")
    raw_tasks = _load_raw_tasks(source_root)
    if len(raw_tasks) != EXPECTED_TASK_COUNT:
        raise ApexImportError(
            f"APEX source must contain exactly {EXPECTED_TASK_COUNT} tasks"
        )
    if len({task.task_id for task in raw_tasks}) != len(raw_tasks):
        raise ApexImportError("APEX source contains duplicate task ids")
    _validate_asset_layout(source_root, raw_tasks)
    return raw_tasks


def _validate_real_source(source_root: Path) -> tuple[_RawTask, ...]:
    """Validate the published data/dev.jsonl + world + task_files layout."""

    _reject_nonfictional_markers(
        {"source": APEX_ACCOUNTING_REPOSITORY, "layout": _REAL_TASK_DATA},
        "source metadata",
    )
    raw_tasks = _load_real_raw_tasks(source_root)
    if len(raw_tasks) != EXPECTED_TASK_COUNT:
        raise ApexImportError(
            f"APEX source must contain exactly {EXPECTED_TASK_COUNT} tasks"
        )
    if len({task.task_id for task in raw_tasks}) != len(raw_tasks):
        raise ApexImportError("APEX source contains duplicate task ids")
    if len({task.source_task_name for task in raw_tasks}) != len(raw_tasks):
        raise ApexImportError("APEX source contains duplicate task names")
    _validate_real_asset_layout(source_root, raw_tasks)
    return raw_tasks


def _load_raw_tasks(source_root: Path) -> tuple[_RawTask, ...]:
    if (source_root / _REAL_TASK_DATA).is_file():
        return _load_real_raw_tasks(source_root)
    path = next(
        (
            source_root / name
            for name in _TASK_FILENAMES
            if (source_root / name).is_file()
        ),
        None,
    )
    if path is None:
        raise ApexImportError("APEX source is missing tasks_and_rubrics.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ApexImportError("APEX task data is malformed JSON") from error
    records: object
    if isinstance(payload, dict):
        records = payload.get("tasks", payload.get("data"))
    else:
        records = payload
    if isinstance(records, dict):
        records = [
            {"task_id": identifier, **record}
            if isinstance(record, dict) and "task_id" not in record
            else record
            for identifier, record in sorted(records.items())
        ]
    if not isinstance(records, list):
        raise ApexImportError("APEX task data must contain a task list")
    parsed: list[_RawTask] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ApexImportError(f"APEX task at index {index} is not an object")
        parsed.append(_parse_raw_task(cast(JsonMapping, record), index))
    return tuple(parsed)


def _load_real_raw_tasks(source_root: Path) -> tuple[_RawTask, ...]:
    """Parse public JSONL rows directly, without requiring invented sidecar files."""

    path = source_root / _REAL_TASK_DATA
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ApexImportError("APEX source is missing data/dev.jsonl") from error
    parsed: list[_RawTask] = []
    for index, line in enumerate(lines):
        if not line.strip():
            raise ApexImportError(f"APEX task row {index} is blank")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ApexImportError(f"APEX task row {index} is malformed JSON") from error
        if not isinstance(record, dict):
            raise ApexImportError(f"APEX task row {index} is not an object")
        parsed.append(
            _parse_raw_task(cast(JsonMapping, record), index, real_layout=True)
        )
    return tuple(parsed)


def _parse_raw_task(
    record: JsonMapping, index: int, *, real_layout: bool = False
) -> _RawTask:
    _reject_stateful_payload(record, f"task {index}")
    _reject_nonfictional_markers(record, f"task {index}")
    task_id = _require_safe_identifier(
        record.get("task_id", record.get("id")), "task id"
    )
    world_id = _require_safe_identifier(record.get("world_id"), "world id")
    prompt = _source_instruction(record, task_id)
    criteria = _parse_rubric(record.get("rubric", record.get("criteria")), task_id)
    gold_output = record.get("gold_output")
    if gold_output is not None and (
        not isinstance(gold_output, str) or not gold_output
    ):
        raise ApexImportError(f"task {task_id!r} has malformed gold_output")
    metadata = record.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ApexImportError(f"task {task_id!r} has malformed source metadata")
    source_task_name = (
        _require_source_task_name(record.get("task_name"), task_id)
        if real_layout
        else task_id
    )
    return _RawTask(
        task_id=task_id,
        source_task_name=source_task_name,
        world_id=world_id,
        prompt=prompt,
        criteria=criteria,
        gold_output=gold_output if isinstance(gold_output, str) else None,
        source_metadata=cast(JsonMapping, metadata),
        world_files=(
            _parse_path_list(record.get("world_files"), task_id, "world file")
            if not real_layout
            else ()
        ),
        task_files=(
            _parse_path_list(record.get("task_files"), task_id, "task file")
            if not real_layout
            else ()
        ),
        context_files=(
            _parse_path_list(record.get("context_files"), task_id, "context file")
            if real_layout
            else ()
        ),
    )


def _require_source_task_name(value: object, task_id: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ApexImportError(f"task {task_id!r} has an invalid source task name")
    return value


def _source_instruction(record: JsonMapping, task_id: str) -> str:
    """Accept public prompt/instruction aliases while rejecting contradictory values."""

    candidates = [
        value
        for field in ("prompt", "instruction", "task_prompt", "task_description")
        if isinstance(value := record.get(field), str) and value.strip()
    ]
    if not candidates:
        raise ApexImportError(f"task {task_id!r} has an empty prompt")
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise ApexImportError(f"task {task_id!r} has contradictory prompt fields")
    return candidates[0]


def _parse_rubric(value: object, task_id: str) -> tuple[JudgeCriterion, ...]:
    if not isinstance(value, list) or not value:
        raise ApexImportError(
            f"task {task_id!r} must contain at least one rubric criterion"
        )
    criteria: list[JudgeCriterion] = []
    identifiers: set[str] = set()
    for index, item in enumerate(value):
        if isinstance(item, str):
            identifier = f"criterion-{index + 1}"
            prompt = item
        elif isinstance(item, dict):
            identifier = _require_safe_identifier(
                item.get("id", f"criterion-{index + 1}"), "rubric criterion id"
            )
            candidate = item.get(
                "description",
                item.get("criterion", item.get("prompt", item.get("text"))),
            )
            prompt = candidate if isinstance(candidate, str) else ""
        else:
            raise ApexImportError(f"task {task_id!r} has malformed rubric criterion")
        if not prompt.strip():
            raise ApexImportError(f"task {task_id!r} has an empty rubric criterion")
        if identifier in identifiers:
            raise ApexImportError(
                f"task {task_id!r} has duplicate rubric criterion ids"
            )
        identifiers.add(identifier)
        criteria.append(
            JudgeCriterion(
                id=identifier,
                prompt=prompt,
                target="final_summary",
                scale="binary",
                weight=1.0,
            )
        )
    return tuple(criteria)


def _parse_path_list(value: object, task_id: str, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ApexImportError(f"task {task_id!r} must declare a {label} list")
    paths = tuple(_safe_relative_path(item, label) for item in value)
    if len(set(paths)) != len(paths):
        raise ApexImportError(f"task {task_id!r} has duplicate {label} references")
    return paths


def _reject_nonfictional_markers(value: object, context: str) -> None:
    """Reject explicit production/secret material without guessing at ordinary names."""

    text = json.dumps(value, ensure_ascii=False).lower()
    markers = (
        "real client",
        "production client",
        "live customer",
        "private key",
        "api_key",
        "api key",
        "aws_secret_access_key",
        "social security number",
    )
    if any(marker in text for marker in markers):
        raise ApexImportError(f"{context} fails fictional-data and secret screening")


def _reject_stateful_payload(value: object, context: str) -> None:
    """Keep an external static task from smuggling stateful scoped world data."""

    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_STATEFUL_FIELDS.intersection(value)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ApexImportError(
                f"{context} contains unsupported stateful fields: {names}"
            )
        for nested in value.values():
            _reject_stateful_payload(nested, context)
    elif isinstance(value, list):
        for nested in value:
            _reject_stateful_payload(nested, context)


def _self_test_public_gold_no_leak(tasks: Iterable[_RawTask]) -> None:
    """Validate public reference answers without ever writing them to task artefacts."""

    for task in tasks:
        gold_output = task.gold_output
        if gold_output is None:
            raise ApexImportError(
                f"task {task.task_id!r} is missing public gold_output"
            )
        if gold_output in task.prompt or any(
            gold_output in criterion.prompt for criterion in task.criteria
        ):
            raise ApexImportError(
                f"task {task.task_id!r} leaks gold output into model-visible text"
            )
        if gold_output in json.dumps(task.source_metadata, ensure_ascii=False):
            raise ApexImportError(
                f"task {task.task_id!r} leaks gold output into source metadata"
            )


def _self_test_public_gold_no_leak_in_artifacts(
    destination: Path, tasks: Iterable[_RawTask]
) -> None:
    """Fail closed if a gold answer reaches any normal installed-pack artefact."""

    gold_values = tuple(
        task.gold_output for task in tasks if task.gold_output is not None
    )
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.is_relative_to(destination / "source"):
            continue
        try:
            contents = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(gold in contents for gold in gold_values):
            raise ApexImportError(
                "gold output leaked into a public installed-pack artefact"
            )


def _validate_asset_layout(source_root: Path, tasks: Sequence[_RawTask]) -> None:
    world_root = source_root / _LEGACY_WORLD_ROOT
    task_root = source_root / "task_files"
    world_paths = _safe_file_paths(world_root, "world")
    task_paths = _safe_file_paths(task_root, "task")
    if len(world_paths) != EXPECTED_WORLD_FILE_COUNT:
        raise ApexImportError(
            f"APEX source must contain exactly {EXPECTED_WORLD_FILE_COUNT} world files"
        )
    if len(task_paths) != EXPECTED_TASK_FILE_COUNT:
        raise ApexImportError(
            f"APEX source must contain exactly {EXPECTED_TASK_FILE_COUNT} task files"
        )
    for task in tasks:
        if any(
            PurePosixPath(path).parts[0] != task.world_id for path in task.world_files
        ):
            raise ApexImportError(
                f"task {task.task_id!r} references a contradictory world file"
            )
    referenced_world_paths = {path for task in tasks for path in task.world_files}
    if referenced_world_paths != set(world_paths):
        raise ApexImportError(
            "world file references are dangling, incomplete, or contradictory"
        )
    referenced_task_paths = {
        f"{task.task_id}/{path}" for task in tasks for path in task.task_files
    }
    if referenced_task_paths != set(task_paths):
        raise ApexImportError(
            "task file references are dangling, incomplete, or contradictory"
        )
    for task in tasks:
        sources = [
            _SourceAsset(
                source_path=f"{_LEGACY_WORLD_ROOT}/{path}",
                runtime_path=PurePosixPath(path).name,
                origin="world",
            )
            for path in task.world_files
        ]
        sources.extend(
            _SourceAsset(
                source_path=f"task_files/{task.task_id}/{path}",
                runtime_path=PurePosixPath(path).name,
                origin="task_files",
            )
            for path in task.task_files
        )
        _assign_task_assets(task, sources)


def _validate_real_asset_layout(source_root: Path, tasks: Sequence[_RawTask]) -> None:
    """Resolve every JSONL context reference to exactly one real source asset."""

    world_paths = _safe_file_paths(source_root / _REAL_WORLD_ROOT, "world")
    task_paths = _safe_file_paths(source_root / _REAL_TASK_ROOT, "task")
    if len(world_paths) != EXPECTED_WORLD_FILE_COUNT:
        raise ApexImportError(
            f"APEX source must contain exactly {EXPECTED_WORLD_FILE_COUNT} world files"
        )
    if len(task_paths) != EXPECTED_TASK_FILE_COUNT:
        raise ApexImportError(
            f"APEX source must contain exactly {EXPECTED_TASK_FILE_COUNT} task files"
        )
    assets = [
        _SourceAsset(
            source_path=f"{_REAL_WORLD_ROOT}/{path}",
            runtime_path=PurePosixPath(path).name,
            origin="world",
        )
        for path in world_paths
    ]
    assets.extend(
        _SourceAsset(
            source_path=f"{_REAL_TASK_ROOT}/{path}",
            runtime_path=PurePosixPath(path).name,
            origin="task_files",
        )
        for path in task_paths
    )
    by_source = {asset.source_path: asset for asset in assets}
    by_runtime: dict[str, list[_SourceAsset]] = {}
    for asset in assets:
        by_runtime.setdefault(_canonical_collision_key(asset.runtime_path), []).append(
            asset
        )
    for task in tasks:
        selected: list[_SourceAsset] = []
        for reference in task.context_files:
            candidates = _context_reference_candidates(reference, by_source, by_runtime)
            if not candidates:
                raise ApexImportError(
                    f"task {task.task_id!r} has a dangling context file reference"
                )
            if len(candidates) != 1:
                raise ApexImportError(
                    f"task {task.task_id!r} has an ambiguous context file reference"
                )
            selected.append(candidates[0])
        _assign_task_assets(task, selected)


def _context_reference_candidates(
    reference: str,
    by_source: Mapping[str, _SourceAsset],
    by_runtime: Mapping[str, Sequence[_SourceAsset]],
) -> Sequence[_SourceAsset]:
    """Resolve an APEX reference, accepting only exact or uniquely flat matches."""

    if reference.startswith(f"{_REAL_WORLD_ROOT}/") or reference.startswith(
        f"{_REAL_TASK_ROOT}/"
    ):
        asset = by_source.get(reference)
        return () if asset is None else (asset,)
    if "/" in reference:
        return ()
    return by_runtime.get(_canonical_collision_key(reference), ())


def _assign_task_assets(task: _RawTask, assets: Sequence[_SourceAsset]) -> None:
    """Mount each selected source once, refusing flat-root name ambiguities."""

    source_paths = [asset.source_path for asset in assets]
    if len(set(source_paths)) != len(source_paths):
        raise ApexImportError(
            f"task {task.task_id!r} has duplicate context file references"
        )
    runtime_keys = [_canonical_collision_key(asset.runtime_path) for asset in assets]
    if len(set(runtime_keys)) != len(runtime_keys):
        raise ApexImportError(
            f"task {task.task_id!r} has conflicting flat-root asset names"
        )
    task.assets = tuple(assets)


def _canonical_collision_key(value: str) -> str:
    """Use one portable key for lookup and collision checks across mounted files."""

    return unicodedata.normalize("NFC", value).casefold()


def _safe_file_paths(root: Path, label: str) -> tuple[str, ...]:
    if not root.is_dir():
        raise ApexImportError(f"APEX source is missing {label} files")
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ApexImportError(
                f"APEX source contains an unsafe {label} file symlink"
            )
        if path.is_file():
            paths.append(
                _safe_relative_path(path.relative_to(root).as_posix(), f"{label} file")
            )
    return tuple(paths)


def _write_static_tasks(
    destination: Path,
    source_root: Path,
    raw_tasks: Iterable[_RawTask],
    revision: str,
) -> None:
    task_root = destination / "tasks"
    task_root.mkdir()
    for raw in sorted(raw_tasks, key=lambda item: item.task_id):
        runtime_files = _write_runtime_assets(destination, source_root, raw)
        static = StaticTaskEpisode(
            episode_id=f"epi-apex-{raw.task_id}",
            task_id=raw.task_id,
            source_task_name=raw.source_task_name,
            world_id=raw.world_id,
            instruction=raw.prompt,
            qualitative_criteria=raw.criteria,
            allowed_tools=APEX_ALLOWED_TOOLS,
            budget=StaticTaskBudget(),
            world_files=tuple(
                item.runtime_path for item in runtime_files if item.origin == "world"
            ),
            task_files=tuple(
                item.runtime_path
                for item in runtime_files
                if item.origin == "task_files"
            ),
            runtime_files=runtime_files,
            source_revision=revision,
        )
        _write_json(task_root / f"{raw.task_id}.json", static.model_dump(mode="json"))


def _write_runtime_assets(
    destination: Path, source_root: Path, raw: _RawTask
) -> tuple[RuntimeFile, ...]:
    """Copy selected files alone into one flat, task-specific, read-only root."""

    runtime_root = destination / "runtime" / raw.task_id
    runtime_root.mkdir(parents=True)
    result: list[RuntimeFile] = []
    for asset in raw.assets:
        source = _source_asset_path(source_root, asset.source_path)
        target = runtime_root / asset.runtime_path
        if target.exists():
            raise ApexImportError("static task runtime asset path is ambiguous")
        shutil.copyfile(source, target)
        data = target.read_bytes()
        source_data = source.read_bytes()
        if data != source_data:
            raise ApexImportError("static task runtime asset copy verification failed")
        target.chmod(0o444)
        result.append(
            RuntimeFile(
                runtime_path=asset.runtime_path,
                origin=asset.origin,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return tuple(result)


def _source_asset_path(source_root: Path, relative: str) -> Path:
    path = source_root / _safe_relative_path(relative, "source asset")
    try:
        path.resolve().relative_to(source_root.resolve())
    except ValueError as error:
        raise ApexImportError(
            "source asset escapes the downloaded source root"
        ) from error
    if not path.is_file() or path.is_symlink():
        raise ApexImportError("source asset is missing or unsafe")
    return path


def _write_manifest(
    destination: Path,
    source_root: Path,
    raw_tasks: Iterable[_RawTask],
    revision: str,
) -> ApexPackManifest:
    entries = _source_files(destination / "source")
    source_digest = _digest_source_entries(entries)
    task_artifacts = _generated_task_artifacts(destination)
    sorted_tasks = tuple(sorted(raw_tasks, key=lambda item: item.task_id))
    mappings = tuple(
        TaskAssetMapping(
            task_id=raw.task_id,
            source_path=asset.source_path,
            runtime_path=asset.runtime_path,
            origin=asset.origin,
            sha256=_source_file_hash(source_root, asset.source_path),
            size_bytes=_source_file_size(source_root, asset.source_path),
        )
        for raw in sorted_tasks
        for asset in raw.assets
    )
    return ApexPackManifest(
        revision=revision,
        contamination="public-reference-answers",
        report_label=APEX_ACCOUNTING_LABEL,
        task_count=EXPECTED_TASK_COUNT,
        world_file_count=EXPECTED_WORLD_FILE_COUNT,
        task_file_count=EXPECTED_TASK_FILE_COUNT,
        source_files=entries,
        source_digest=source_digest,
        task_artifacts=task_artifacts,
        criterion_count=sum(len(task.criteria) for task in sorted_tasks),
        source_metadata={
            task.task_id: _json_object(task.source_metadata) for task in sorted_tasks
        },
        task_asset_mappings=mappings,
    )


def _source_file_hash(source_root: Path, relative: str) -> str:
    return hashlib.sha256(
        _source_asset_path(source_root, relative).read_bytes()
    ).hexdigest()


def _source_file_size(source_root: Path, relative: str) -> int:
    return _source_asset_path(source_root, relative).stat().st_size


def _json_object(value: Mapping[str, object]) -> dict[str, JsonValue]:
    """Validate retained source metadata as ordinary JSON, never an opaque object."""

    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ApexImportError(
            "APEX source metadata must contain JSON values"
        ) from error
    if not isinstance(
        decoded, dict
    ):  # pragma: no cover - Mapping always encodes as object
        raise ApexImportError("APEX source metadata must be a JSON object")
    return cast(dict[str, JsonValue], decoded)


def _source_files(source_root: Path) -> tuple[SourceFile, ...]:
    files: list[SourceFile] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise ApexImportError("installed source contains a symbolic link")
        if not path.is_file():
            continue
        data = path.read_bytes()
        files.append(
            SourceFile(
                path=_safe_relative_path(
                    path.relative_to(source_root).as_posix(), "source file"
                ),
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return tuple(files)


def _generated_task_artifacts(root: Path) -> tuple[GeneratedTaskArtifact, ...]:
    """Hash exactly the flat task JSON documents generated by this installer."""

    task_root = root / "tasks"
    if not task_root.is_dir() or task_root.is_symlink():
        raise ApexImportError("installed static-task artifact directory is missing")
    artifacts: list[GeneratedTaskArtifact] = []
    for path in sorted(task_root.rglob("*")):
        if path.is_symlink():
            raise ApexImportError("installed static-task artifact is a symbolic link")
        if path.is_dir():
            raise ApexImportError(
                "installed static-task artifact directory is unexpected"
            )
        if not path.is_file():
            raise ApexImportError("installed static-task artifact is unsupported")
        relative_path = _safe_relative_path(
            path.relative_to(root).as_posix(), "static task artifact"
        )
        parts = PurePosixPath(relative_path).parts
        if len(parts) != 2 or parts[0] != "tasks" or path.suffix != ".json":
            raise ApexImportError("installed static-task artifact path is unsupported")
        artifacts.append(
            GeneratedTaskArtifact(
                relative_path=relative_path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return tuple(artifacts)


def _digest_source_entries(entries: Sequence[SourceFile]) -> str:
    return hashlib.sha256(
        canonical_json([entry.model_dump(mode="json") for entry in entries]).encode(
            "utf-8"
        )
    ).hexdigest()


def _write_lock(destination: Path, manifest: ApexPackManifest) -> None:
    manifest_bytes = (destination / "pack-manifest.json").read_bytes()
    lock = ApexPackLock(
        revision=manifest.revision,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_digest=manifest.source_digest,
    )
    _write_json(
        destination.parent / "apex-accounting.lock.json", lock.model_dump(mode="json")
    )


def _verify_source_files(root: Path, manifest: ApexPackManifest) -> None:
    entries = _source_files(root / "source")
    if entries != manifest.source_files:
        raise ApexImportError("installed APEX source file hash verification failed")
    if _digest_source_entries(entries) != manifest.source_digest:
        raise ApexImportError("installed APEX source digest verification failed")


def _verify_task_artifacts(
    root: Path, manifest: ApexPackManifest
) -> tuple[GeneratedTaskArtifact, ...]:
    """Require the exact generated task set and bytes recorded before manifest write."""

    return tuple(
        artifact for artifact, _, _ in _load_verified_task_artifacts(root, manifest)
    )


def _load_verified_task_artifacts(
    root: Path, manifest: ApexPackManifest
) -> tuple[tuple[GeneratedTaskArtifact, bytes, StaticTaskEpisode], ...]:
    """Read, hash, and parse each generated task from one immutable byte stream."""

    expected = manifest.task_artifacts
    expected_paths = [artifact.relative_path for artifact in expected]
    if (
        len(expected) != manifest.task_count
        or len(set(expected_paths)) != len(expected_paths)
        or tuple(sorted(expected_paths)) != tuple(expected_paths)
    ):
        raise ApexImportError("pack manifest static-task artifact metadata is invalid")
    actual_paths = _generated_task_artifact_paths(root)
    if actual_paths != expected_paths:
        raise ApexImportError(
            "installed static-task artifact set does not match manifest"
        )
    loaded: list[tuple[GeneratedTaskArtifact, bytes, StaticTaskEpisode]] = []
    for artifact in expected:
        path = root / artifact.relative_path
        data = _read_verified_regular_file(path, "static task artifact")
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise ApexImportError(
                "installed static-task artifact hash verification failed"
            )
        try:
            task = StaticTaskEpisode.model_validate_json(data)
        except (ValidationError, json.JSONDecodeError) as error:
            raise ApexImportError(f"static task {path.name!r} is invalid") from error
        if task.source_revision != manifest.revision:
            raise ApexImportError(
                "static task source revision contradicts pack manifest"
            )
        if artifact.relative_path != f"tasks/{task.task_id}.json":
            raise ApexImportError(
                "static task identity contradicts its generated artifact path"
            )
        loaded.append((artifact, data, task))
    return tuple(loaded)


def _generated_task_artifact_paths(root: Path) -> list[str]:
    """List the exact supported task-document paths without reopening their bytes."""

    task_root = root / "tasks"
    if not task_root.is_dir() or task_root.is_symlink():
        raise ApexImportError("installed static-task artifact directory is missing")
    paths: list[str] = []
    for path in sorted(task_root.iterdir()):
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise ApexImportError(
                "installed static-task artifact is unavailable"
            ) from error
        if stat.S_ISLNK(mode):
            raise ApexImportError("installed static-task artifact is a symbolic link")
        if not stat.S_ISREG(mode):
            raise ApexImportError("installed static-task artifact is unsupported")
        relative_path = _safe_relative_path(
            path.relative_to(root).as_posix(), "static task artifact"
        )
        parts = PurePosixPath(relative_path).parts
        if len(parts) != 2 or parts[0] != "tasks" or path.suffix != ".json":
            raise ApexImportError("installed static-task artifact path is unsupported")
        paths.append(relative_path)
    return paths


def _read_verified_regular_file(path: Path, label: str) -> bytes:
    """Read one no-follow regular file from one descriptor, rejecting substitutions."""

    flags = os.O_RDONLY
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(no_follow, int):
        raise ApexImportError(f"{label} cannot be read without following links")
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not isinstance(nonblocking, int):
        raise ApexImportError(f"{label} cannot be read safely")
    flags |= no_follow | nonblocking
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ApexImportError(f"{label} is missing or unsafe") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ApexImportError(f"{label} is missing or unsafe")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    except OSError as error:
        raise ApexImportError(f"{label} could not be read safely") from error
    finally:
        os.close(descriptor)


def _verify_lock(root: Path, manifest: ApexPackManifest, manifest_bytes: bytes) -> None:
    path = root.parent / "apex-accounting.lock.json"
    lock_bytes = _read_verified_regular_file(path, "APEX pack lockfile")
    try:
        lock = ApexPackLock.model_validate_json(lock_bytes)
    except (ValidationError, json.JSONDecodeError) as error:
        raise ApexImportError("APEX pack lockfile is missing or invalid") from error
    if (
        lock.revision != manifest.revision
        or lock.source_digest != manifest.source_digest
        or lock.manifest_sha256 != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        raise ApexImportError("APEX pack lockfile contradicts the installed manifest")


def _validate_static_task_assets(
    task: StaticTaskEpisode, runtime_bytes: Mapping[str, bytes]
) -> None:
    if task.allowed_tools != APEX_ALLOWED_TOOLS:
        raise ApexImportError("static task declares unsupported tools")
    names = [asset.runtime_path for asset in task.runtime_files]
    if len({_canonical_collision_key(name) for name in names}) != len(names):
        raise ApexImportError("static task has ambiguous flat-root asset names")
    if tuple(
        asset.runtime_path for asset in task.runtime_files if asset.origin == "world"
    ) != (task.world_files):
        raise ApexImportError(
            "static task world asset metadata contradicts runtime assets"
        )
    if (
        tuple(
            asset.runtime_path
            for asset in task.runtime_files
            if asset.origin == "task_files"
        )
        != task.task_files
    ):
        raise ApexImportError(
            "static task task-file metadata contradicts runtime assets"
        )
    if set(runtime_bytes) != set(names):
        raise ApexImportError(
            "static task runtime asset set does not match its verified task"
        )
    for asset in task.runtime_files:
        data = runtime_bytes[asset.runtime_path]
        if (
            len(data) != asset.size_bytes
            or hashlib.sha256(data).hexdigest() != asset.sha256
        ):
            raise ApexImportError("static task runtime asset hash verification failed")


def _read_verified_runtime_assets(
    root: Path, task: StaticTaskEpisode
) -> dict[str, bytes]:
    """Capture every authorised runtime asset before a provider can be contacted."""

    runtime_root = root / "runtime" / task.task_id
    _validate_runtime_asset_set(runtime_root, task.runtime_files)
    captured: dict[str, bytes] = {}
    for asset in task.runtime_files:
        path = runtime_root / _safe_relative_path(asset.runtime_path, "asset")
        data = _read_verified_regular_file(path, "static task runtime asset")
        if (
            len(data) != asset.size_bytes
            or hashlib.sha256(data).hexdigest() != asset.sha256
        ):
            raise ApexImportError("static task runtime asset hash verification failed")
        captured[asset.runtime_path] = data
    return captured


def _validate_runtime_asset_set(root: Path, assets: Sequence[RuntimeFile]) -> None:
    """Require a task's flat runtime root to contain exactly its declared files."""

    if not root.is_dir() or root.is_symlink():
        raise ApexImportError("static task runtime asset root is missing or unsafe")
    actual: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ApexImportError("static task runtime asset set contains a symlink")
        if path.is_dir():
            raise ApexImportError("static task runtime asset set contains a directory")
        if not path.is_file():
            raise ApexImportError(
                "static task runtime asset set contains an unsupported entry"
            )
        relative = _safe_relative_path(
            path.relative_to(root).as_posix(), "runtime asset"
        )
        if len(PurePosixPath(relative).parts) != 1:
            raise ApexImportError("static task runtime asset set is not flat")
        actual.append(relative)
    expected = [asset.runtime_path for asset in assets]
    if len({_canonical_collision_key(path) for path in actual}) != len(
        actual
    ) or sorted(actual) != sorted(expected):
        raise ApexImportError(
            "static task runtime asset set does not match its verified task"
        )


def _validate_runtime_task_roots(
    runtime_root: Path, tasks: Sequence[StaticTaskEpisode]
) -> None:
    """Reject entries for unknown tasks before any static task can execute."""

    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ApexImportError("static task runtime root is missing or unsafe")
    actual: list[str] = []
    for path in sorted(runtime_root.iterdir()):
        if path.is_symlink() or not path.is_dir():
            raise ApexImportError("static task runtime root contains an unsafe entry")
        actual.append(path.name)
    expected = sorted(task.task_id for task in tasks)
    if sorted(actual) != expected:
        raise ApexImportError("static task runtime root has unexpected task assets")


def _validate_manifest_asset_mappings(
    manifest: ApexPackManifest, tasks: Sequence[StaticTaskEpisode]
) -> None:
    """Tie each agent-visible mount to a source-provenance manifest entry."""

    expected = {
        (
            mapping.task_id,
            mapping.runtime_path,
            mapping.origin,
            mapping.sha256,
            mapping.size_bytes,
        )
        for mapping in manifest.task_asset_mappings
    }
    actual = {
        (
            task.task_id,
            asset.runtime_path,
            asset.origin,
            asset.sha256,
            asset.size_bytes,
        )
        for task in tasks
        for asset in task.runtime_files
    }
    if actual != expected:
        raise ApexImportError(
            "static task runtime assets contradict manifest provenance"
        )
    if {task.task_id for task in tasks} != set(manifest.source_metadata):
        raise ApexImportError("static task metadata contradicts the installed manifest")


def _read_json_mapping(path: Path, label: str) -> JsonMapping:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ApexImportError(f"APEX {label} is missing or malformed") from error
    if not isinstance(value, dict):
        raise ApexImportError(f"APEX {label} must be a JSON object")
    return cast(JsonMapping, value)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
