"""Deterministic, atomic persistence for WP-13 run and aggregate artifacts.

The stable public per-run score remains the §E.17 ``EvaluationResult`` in
``scores.json``.  This module deliberately stores execution provenance beside it in a
separate envelope rather than extending the stable core schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TextIO, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from mirrorfirm.core.db import SQLiteWorldView
from mirrorfirm.core.digest import canonical_json
from mirrorfirm.core.models import (
    Action,
    EvaluationResult,
    LayerScores,
    StateSnapshot,
    Usage,
)
from mirrorfirm.security import (
    SanitizationError,
    canonical_sanitized_json,
    is_secret_key,
    sanitize_for_persistence,
    sanitize_text,
    sanitize_transcript_bytes,
)


class ResultArtifactError(ValueError):
    """A persisted result artifact is incomplete, malformed, or incompatible."""


@dataclass(frozen=True)
class _DirectoryIdentity:
    """One visible ancestor which must keep its exact directory identity."""

    path: Path
    device: int
    inode: int


def _directory_identity_chain(path: Path) -> tuple[_DirectoryIdentity, ...]:
    """Capture every real-directory component without resolving user paths.

    A retained descriptor protects operations relative to its directory, while this
    chain makes a later visible ancestor substitution observable before a subsystem
    that receives ordinary paths (the compiler and provider adapters) is entered.
    """

    if not path.is_absolute():  # pragma: no cover - callers normalize first
        raise ResultArtifactError("execution output directory path is unsafe")
    current = Path(path.anchor)
    identities: list[_DirectoryIdentity] = []
    for part in path.parts[1:]:
        current /= part
        try:
            visible = os.lstat(current)
            resolved = os.stat(current)
        except OSError as error:
            raise ResultArtifactError(
                "execution output directory is missing or unsafe"
            ) from error
        alias = _is_macos_var_alias(current)
        if (
            (stat.S_ISLNK(visible.st_mode) and not alias)
            or (not stat.S_ISDIR(visible.st_mode) and not alias)
            or not stat.S_ISDIR(resolved.st_mode)
        ):
            raise ResultArtifactError("execution output directory is unsafe")
        identities.append(_DirectoryIdentity(current, resolved.st_dev, resolved.st_ino))
    return tuple(identities)


class SecureExecutionFile:
    """A retained no-follow descriptor for one runner-side output file."""

    def __init__(
        self, parent: "SecureExecutionDirectory", name: str, descriptor: int
    ) -> None:
        self.parent = parent
        self.name = name
        self.path = parent.path / name
        self._descriptor = descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResultArtifactError("execution output file is unsafe")
        self._identity = (metadata.st_dev, metadata.st_ino)

    def close(self) -> None:
        """Release the retained file descriptor."""

        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def verify_current(self) -> None:
        """Ensure the visible child still names the retained regular file."""

        if self._descriptor < 0:
            raise ResultArtifactError("execution output file is closed")
        self.parent.verify_current()
        metadata = os.fstat(self._descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self._identity
        ):
            raise ResultArtifactError("execution output file was replaced or is unsafe")
        try:
            visible = os.lstat(self.name, dir_fd=self.parent._descriptor)
            resolved = os.stat(self.name, dir_fd=self.parent._descriptor)
        except OSError as error:
            raise ResultArtifactError(
                "execution output file is missing or unsafe"
            ) from error
        if (
            stat.S_ISLNK(visible.st_mode)
            or not stat.S_ISREG(visible.st_mode)
            or (resolved.st_dev, resolved.st_ino) != self._identity
        ):
            raise ResultArtifactError("execution output file was replaced or is unsafe")

    def open_text_writer(self) -> TextIO:
        """Open a duplicate of the retained descriptor for one UTF-8 transcript."""

        self.verify_current()
        return os.fdopen(os.dup(self._descriptor), "w", encoding="utf-8")


class SecureExecutionDirectory:
    """A retained no-follow directory descriptor for runner-side artifacts."""

    def __init__(
        self,
        path: Path,
        descriptor: int,
        *,
        ancestor_identities: tuple[_DirectoryIdentity, ...] | None = None,
    ) -> None:
        self.path = path
        self._descriptor = descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ResultArtifactError("execution output directory is unsafe")
        self._identity = (metadata.st_dev, metadata.st_ino)
        identities = (
            _directory_identity_chain(path)
            if ancestor_identities is None
            else ancestor_identities
        )
        if (
            not identities
            or (
                identities[-1].device,
                identities[-1].inode,
            )
            != self._identity
        ):
            raise ResultArtifactError(
                "execution output directory was replaced or is unsafe"
            )
        self._ancestor_identities = identities

    def close(self) -> None:
        """Release the retained directory descriptor."""

        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def verify_current(self) -> None:
        """Reject path replacement before a path-only subsystem is invoked."""

        if self._descriptor < 0:
            raise ResultArtifactError("execution output directory is closed")
        metadata = os.fstat(self._descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self._identity
        ):
            raise ResultArtifactError(
                "execution output directory was replaced or is unsafe"
            )
        for identity in self._ancestor_identities:
            try:
                visible = os.lstat(identity.path)
                resolved = os.stat(identity.path)
            except OSError as error:
                raise ResultArtifactError(
                    "execution output directory is missing or unsafe"
                ) from error
            alias = _is_macos_var_alias(identity.path)
            if (
                (stat.S_ISLNK(visible.st_mode) and not alias)
                or (not stat.S_ISDIR(visible.st_mode) and not alias)
                or not stat.S_ISDIR(resolved.st_mode)
                or (resolved.st_dev, resolved.st_ino)
                != (identity.device, identity.inode)
            ):
                raise ResultArtifactError(
                    "execution output directory was replaced or is unsafe"
                )

    def checkpoint(self) -> None:
        """Assert that the retained directory and every secured ancestor still match."""

        self.verify_current()

    @property
    def descriptor(self) -> int:
        """Return the retained descriptor only for descriptor-relative operations."""

        if self._descriptor < 0:
            raise ResultArtifactError("execution output directory is closed")
        return self._descriptor

    def verify_child_directory(
        self, name: str, child: "SecureExecutionDirectory"
    ) -> None:
        """Ensure a visible child remains the exact retained directory inode."""

        self.verify_current()
        child.verify_current()
        try:
            visible = os.lstat(name, dir_fd=self._descriptor)
            resolved = os.stat(name, dir_fd=self._descriptor)
        except OSError as error:
            raise ResultArtifactError(
                "execution output child directory is missing or unsafe"
            ) from error
        if (
            stat.S_ISLNK(visible.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or (resolved.st_dev, resolved.st_ino) != child._identity
        ):
            raise ResultArtifactError(
                "execution output child directory was replaced or is unsafe"
            )

    def create_child(self, name: str) -> "SecureExecutionDirectory":
        """Create and retain one regular child directory through this descriptor."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output child name is unsafe")
        self.verify_current()
        try:
            os.mkdir(name, 0o700, dir_fd=self._descriptor)
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._descriptor,
            )
        except OSError as error:
            raise ResultArtifactError(
                "execution output directory is unsafe or exists"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError("not a directory")
            child_identity = _DirectoryIdentity(
                self.path / name, metadata.st_dev, metadata.st_ino
            )
            child = SecureExecutionDirectory(
                self.path / name,
                descriptor,
                ancestor_identities=(*self._ancestor_identities, child_identity),
            )
            child.verify_current()
            return child
        except BaseException:
            os.close(descriptor)
            raise

    def open_child(self, name: str) -> "SecureExecutionDirectory":
        """Retain an existing regular child directory without reopening its path."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output child name is unsafe")
        self.checkpoint()
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._descriptor,
            )
        except OSError as error:
            raise ResultArtifactError(
                "execution output child directory is missing or unsafe"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError("not a directory")
            child_identity = _DirectoryIdentity(
                self.path / name, metadata.st_dev, metadata.st_ino
            )
            child = SecureExecutionDirectory(
                self.path / name,
                descriptor,
                ancestor_identities=(*self._ancestor_identities, child_identity),
            )
            self.verify_child_directory(name, child)
            return child
        except BaseException:
            os.close(descriptor)
            raise

    def ensure_child(self, name: str) -> "SecureExecutionDirectory":
        """Create or securely retain one ordinary child directory."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output child name is unsafe")
        self.checkpoint()
        try:
            os.mkdir(name, 0o700, dir_fd=self._descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise ResultArtifactError("execution output directory is unsafe") from error
        return self.open_child(name)

    def descriptor_path(self, name: str) -> Path:
        """Address one child through the retained directory, not a mutable parent path."""

        if not name or Path(name).name != name or self._descriptor < 0:
            raise ResultArtifactError("execution output filename is unsafe")
        return Path("/dev/fd") / str(self._descriptor) / name

    def descriptor_root_path(self) -> Path:
        """Address this retained directory itself through its descriptor."""

        if self._descriptor < 0:
            raise ResultArtifactError("execution output directory is closed")
        return Path("/dev/fd") / str(self._descriptor)

    def create_empty_file_from(self, name: str, source: Path) -> Path:
        """Copy one source file to a no-follow destination through this descriptor."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output filename is unsafe")
        file = self.create_file_from(name, source)
        try:
            file.verify_current()
            return file.path
        finally:
            file.close()

    def create_file_from(self, name: str, source: Path) -> SecureExecutionFile:
        """Copy a source file once and retain the destination descriptor."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output filename is unsafe")
        self.verify_current()
        try:
            source_descriptor = os.open(
                source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
        except OSError as error:
            raise ResultArtifactError("compiled world is missing or unsafe") from error
        destination_descriptor: int | None = None
        try:
            if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
                raise ResultArtifactError("compiled world is missing or unsafe")
            destination_descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._descriptor,
            )
            while data := os.read(source_descriptor, 1024 * 1024):
                written = 0
                while written < len(data):
                    written += os.write(destination_descriptor, data[written:])
            os.fsync(destination_descriptor)
        except OSError as error:
            if destination_descriptor is not None:
                os.close(destination_descriptor)
                destination_descriptor = None
                try:
                    os.unlink(name, dir_fd=self._descriptor)
                except FileNotFoundError:
                    pass
            raise ResultArtifactError("execution output file is unsafe") from error
        finally:
            os.close(source_descriptor)
        if destination_descriptor is None:  # pragma: no cover - control-flow guard
            raise ResultArtifactError("execution output file is unsafe")
        try:
            output = SecureExecutionFile(self, name, destination_descriptor)
        except BaseException:
            os.close(destination_descriptor)
            raise
        self.verify_current()
        output.verify_current()
        return output

    def create_empty_file(self, name: str) -> SecureExecutionFile:
        """Create and retain one new regular file through the directory descriptor."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output filename is unsafe")
        self.verify_current()
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._descriptor,
            )
        except OSError as error:
            raise ResultArtifactError("execution output file is unsafe") from error
        try:
            file = SecureExecutionFile(self, name, descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        file.verify_current()
        return file

    def retain_regular_file(self, name: str) -> SecureExecutionFile:
        """Retain an existing regular child after a descriptor-relative writer creates it."""

        if not name or Path(name).name != name:
            raise ResultArtifactError("execution output filename is unsafe")
        self.verify_current()
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._descriptor,
            )
        except OSError as error:
            raise ResultArtifactError("execution output file is unsafe") from error
        try:
            file = SecureExecutionFile(self, name, descriptor)
            file.verify_current()
            return file
        except BaseException:
            os.close(descriptor)
            raise

    def remove_file_if_present(self, name: str) -> None:
        """Discard one owned publication through this retained descriptor only."""

        if not name or Path(name).name != name or self._descriptor < 0:
            return
        try:
            metadata = os.lstat(name, dir_fd=self._descriptor)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            return
        try:
            os.unlink(name, dir_fd=self._descriptor)
        except OSError:
            return

    def clean_partial_contents(self) -> None:
        """Remove only artifacts created under this retained descriptor after failure."""

        if self._descriptor < 0:
            return
        for name in os.listdir(self._descriptor):
            self._remove_child_from_descriptor(self._descriptor, name)

    @classmethod
    def _remove_child_from_descriptor(
        cls, directory_descriptor: int, name: str
    ) -> None:
        """Remove one retained-descriptor child without reopening a visible pathname."""

        try:
            metadata = os.lstat(name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_descriptor,
                )
            except OSError:
                return
            try:
                for child in os.listdir(descriptor):
                    cls._remove_child_from_descriptor(descriptor, child)
            finally:
                os.close(descriptor)
            try:
                os.rmdir(name, dir_fd=directory_descriptor)
            except OSError:
                pass
            return
        try:
            os.unlink(name, dir_fd=directory_descriptor)
        except OSError:
            pass


def open_secure_execution_directory(path: str | Path) -> SecureExecutionDirectory:
    """Create/open a no-follow results root before any runner-side work begins."""

    candidate = Path(path).absolute()
    _ensure_regular_directory(candidate)
    descriptor = _open_regular_directory(candidate)
    try:
        directory = SecureExecutionDirectory(candidate, descriptor)
        directory.checkpoint()
        return directory
    except BaseException:
        os.close(descriptor)
        raise


class SnapshotArtifact(BaseModel):
    """Portable provenance for one snapshot materialised inside a run directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    phase: Literal["initial", "final"]
    state_digest: str
    relative_path: str
    sha256: str


class SnapshotExecutionBinding(BaseModel):
    """Execution-only identities that bind snapshot files to their semantic roles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_run_id: str
    initial_snapshot_id: str
    initial_state_digest: str
    final_snapshot_id: str
    final_state_digest: str
    terminal_action_id: str
    terminal_action_output_digest: str


RunKind = Literal["native_model", "scripted_reference"]
REFERENCE_DISPLAY_LABEL = "reference (scripted) - not model performance"


class PublicRunConfiguration(BaseModel):
    """The explicit, credential-free subset that makes a run reproducible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_version: Literal["mirrorfirm-wp13-v1"] = "mirrorfirm-wp13-v1"
    scoring_version: Literal["wp09-v1", "wp09-v2"] = "wp09-v2"
    episode_id: str
    model_identifier: str
    world_id: str
    world_version: str
    jurisdiction: str = "unknown"
    run_kind: RunKind
    judge_models: tuple[str, ...] = ()
    seed: int | None = None
    temperature: float | None = None
    max_steps: int | None = None
    max_tokens: int | None = None
    max_world_days: float | None = None
    baseline: bool = False


class ComparisonIdentity(BaseModel):
    """All material dimensions that must agree for a native comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_version: str
    scoring_version: str
    episode_id: str
    world_id: str
    world_version: str
    jurisdiction: str
    run_kind: RunKind
    judge_models: tuple[str, ...]
    seed: int | None
    temperature: float | None
    max_steps: int | None
    max_tokens: int | None
    max_world_days: float | None
    baseline: bool


class RunArtifact(BaseModel):
    """Verified, non-secret execution provenance beside a stable E.17 result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["mirrorfirm-run-v1"] = "mirrorfirm-run-v1"
    status: Literal["complete", "incomplete"]
    episode_id: str
    run_id: str
    model: str
    world_id: str
    world_version: str
    run_kind: RunKind
    artifact_id: str
    configuration: PublicRunConfiguration
    configuration_hash: str
    sweep_id: str | None = None
    sweep_run_index: int | None = None
    fresh_world_id: str | None = None
    judge_models: tuple[str, ...] = ()
    seed: int | None = None
    initial_snapshot: SnapshotArtifact
    final_snapshot: SnapshotArtifact
    snapshot_binding: SnapshotExecutionBinding | None = None
    transcript_path: str | None = None
    transcript_sha256: str | None = None
    agent_result: dict[str, JsonValue]
    score_path: str | None = None
    score_sha256: str | None = None
    error: str | None = None


class AggregateArtifact(BaseModel):
    """One compatible native episode/model aggregate for reports and comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["mirrorfirm-aggregate-v1"] = "mirrorfirm-aggregate-v1"
    kind: RunKind
    label: str | None = None
    episode_id: str
    model: str
    world_id: str
    world_version: str
    run_count: int = Field(gt=0)
    configuration: PublicRunConfiguration
    configuration_hash: str
    comparison_identity: ComparisonIdentity
    judge_models: tuple[str, ...] = ()
    runs: tuple[RunArtifact, ...]
    results: tuple[EvaluationResult, ...]
    reliability: dict[str, float | int]
    layer_means: LayerScores
    usage_means: Usage
    critical_failure_ids: tuple[str, ...]


class ExternalApexArtifact(BaseModel):
    """A separately labelled public-reference APEX result, never a native aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["mirrorfirm-apex-result-v1"] = "mirrorfirm-apex-result-v1"
    kind: Literal["external_apex"] = "external_apex"
    source_revision: str
    report_label: str
    contamination: Literal["public-reference-answers"]
    evaluation: dict[str, JsonValue]


_SECRET_ERROR_MARKERS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "private_key",
    "private-key",
    "client_secret",
    "client-secret",
    "cookie",
    "certificate",
    "pem",
)

_PUBLIC_CONFIGURATION_FIELDS = frozenset(
    {
        "suite_version",
        "scoring_version",
        "episode_id",
        "model",
        "model_identifier",
        "world_id",
        "world_version",
        "jurisdiction",
        "run_kind",
        "judge_models",
        "seed",
        "temperature",
        "max_steps",
        "max_tokens",
        "max_world_days",
        "baseline",
    }
)


def canonical_configuration_hash(configuration: Mapping[str, object]) -> str:
    """Hash only canonical public reproducibility configuration.

    This public helper intentionally drops credential-shaped fields for callers such
    as sweep preflight. Persisted run artifacts use ``_public_configuration`` below,
    which additionally rejects unsupported non-secret keys.
    """

    return hashlib.sha256(
        canonical_json(_public_mapping(configuration, reject_unknown=False)).encode(
            "utf-8"
        )
    ).hexdigest()


def write_json_atomic(
    path: str | Path, payload: object, *, output_root: str | Path | None = None
) -> Path:
    """Create one deterministic JSON artifact without silently overwriting a run."""

    try:
        encoded = canonical_sanitized_json(payload)
    except SanitizationError as error:
        raise ResultArtifactError(
            "artifact content cannot be sanitized safely"
        ) from error
    return _write_bytes_atomic(Path(path), encoded, output_root=output_root)


def create_output_directory(path: str | Path) -> Path:
    """Create a new regular output root without following a caller-controlled link."""

    root = Path(path)
    if root.exists() or root.is_symlink():
        raise ResultArtifactError(
            "artifact output directory already exists or is unsafe"
        )
    _ensure_regular_directory(root)
    return root


def write_text_atomic(
    path: str | Path, content: str, *, output_root: str | Path | None = None
) -> Path:
    """Create one UTF-8 report atomically without overwriting an existing output."""

    return _write_bytes_atomic(
        Path(path), sanitize_text(content).encode("utf-8"), output_root=output_root
    )


def _write_bytes_atomic(
    destination: Path, encoded: bytes, *, output_root: str | Path | None = None
) -> Path:
    """Create a file through regular, non-symlink directory components only."""

    root = Path(output_root) if output_root is not None else destination.parent
    _require_contained_destination(destination, root)
    parent_descriptor = _open_regular_directory(destination.parent)
    temporary_name = f".{destination.name}.{os.urandom(16).hex()}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise FileExistsError(
                f"refusing to overwrite completed artifact {destination}"
            ) from error
        except OSError as error:
            raise ResultArtifactError("artifact output is unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        finally:
            os.close(parent_descriptor)
    return destination


def write_json_atomic_to_directory(
    directory: SecureExecutionDirectory, name: str, payload: object
) -> Path:
    """Atomically publish canonical JSON through a retained directory descriptor."""

    try:
        encoded = canonical_sanitized_json(payload)
    except SanitizationError as error:
        raise ResultArtifactError(
            "artifact content cannot be sanitized safely"
        ) from error
    return _write_bytes_atomic_to_directory(directory, name, encoded)


def write_text_atomic_to_directory(
    directory: SecureExecutionDirectory, name: str, content: str
) -> Path:
    """Atomically publish sanitized text through a retained directory descriptor."""

    return _write_bytes_atomic_to_directory(
        directory, name, sanitize_text(content).encode("utf-8")
    )


def _write_bytes_atomic_to_directory(
    directory: SecureExecutionDirectory, name: str, encoded: bytes
) -> Path:
    """Publish one file without reopening a mutable destination pathname."""

    if not name or Path(name).name != name:
        raise ResultArtifactError("execution output filename is unsafe")
    directory.checkpoint()
    temporary_name = f".{name}.{os.urandom(16).hex()}"
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory.descriptor,
        )
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        # The descriptor remains authoritative if an attacker swaps the visible path
        # after this checkpoint; the post-link checkpoint below removes only our file
        # through the retained descriptor.
        directory.checkpoint()
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=directory.descriptor,
                dst_dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            published = True
        except FileExistsError as error:
            raise FileExistsError(
                f"refusing to overwrite completed artifact {directory.path / name}"
            ) from error
        except OSError as error:
            raise ResultArtifactError("artifact output is unsafe") from error
        directory.checkpoint()
        return directory.path / name
    except BaseException:
        if published:
            directory.remove_file_if_present(name)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory.descriptor)
        except (FileNotFoundError, OSError):
            pass


def write_scores_artifact(result: EvaluationResult, destination: str | Path) -> Path:
    """Persist the unmodified E.17 result schema as deterministic canonical JSON."""

    path = Path(destination)
    if path.name != "scores.json":
        path = path / "scores.json"
    return write_json_atomic(path, _canonical_evaluation_payload(result))


def write_run_artifact(
    *,
    run_directory: str | Path,
    episode_id: str,
    model: str,
    world_id: str,
    world_version: str,
    configuration: Mapping[str, object],
    judge_models: Sequence[str],
    run_id: str,
    initial_snapshot: StateSnapshot,
    final_snapshot: StateSnapshot,
    agent_result: Mapping[str, object],
    evaluation: EvaluationResult | None,
    seed: int | None = None,
    error: str | None = None,
    configuration_hash: str | None = None,
    run_kind: RunKind = "native_model",
    jurisdiction: str = "unknown",
    sweep_id: str | None = None,
    sweep_run_index: int | None = None,
    fresh_world_id: str | None = None,
) -> RunArtifact:
    """Atomically mark a run complete only after its stable score has been written."""

    directory = Path(run_directory)
    _ensure_regular_directory(directory)
    if (directory / "run.json").exists() or (directory / "run.json").is_symlink():
        raise FileExistsError(
            f"refusing to overwrite completed artifact {directory / 'run.json'}"
        )
    configured_identifier = configuration.get(
        "model_identifier", configuration.get("model", model)
    )
    if not isinstance(configured_identifier, str):
        raise ResultArtifactError("configuration model identifier is invalid")
    public_configuration = _public_configuration(
        configuration,
        episode_id=episode_id,
        model_identifier=configured_identifier,
        world_id=world_id,
        world_version=world_version,
        judge_models=judge_models,
        seed=seed,
        run_kind=run_kind,
        jurisdiction=jurisdiction,
    )
    computed_configuration_hash = canonical_configuration_hash(
        public_configuration.model_dump(mode="json")
    )
    if (
        configuration_hash is not None
        and configuration_hash != computed_configuration_hash
    ):
        raise ResultArtifactError(
            "provided configuration hash contradicts public configuration"
        )
    _validate_run_identity(model, public_configuration)
    if any(
        value is None for value in (sweep_id, sweep_run_index, fresh_world_id)
    ) and any(
        value is not None for value in (sweep_id, sweep_run_index, fresh_world_id)
    ):
        raise ResultArtifactError(
            "sweep execution provenance must be complete or absent"
        )
    if sweep_run_index is not None and sweep_run_index <= 0:
        raise ResultArtifactError("sweep execution run index is invalid")
    score_path = directory / "scores.json"
    status: Literal["complete", "incomplete"] = (
        "complete" if evaluation is not None else "incomplete"
    )
    if evaluation is not None:
        if evaluation.episode_id != episode_id or evaluation.run_id != run_id:
            raise ResultArtifactError("evaluation does not belong to the persisted run")
        if not score_path.exists():
            write_scores_artifact(evaluation, score_path)
        score_bytes = _read_verified_regular_file(score_path, "scores artifact")
        expected_score = (
            canonical_json(_canonical_evaluation_payload(evaluation)).encode("utf-8")
            + b"\n"
        )
        if score_bytes != expected_score:
            raise ResultArtifactError(
                "scores artifact does not match the evaluation result"
            )
        score_sha256: str | None = hashlib.sha256(score_bytes).hexdigest()
        score_relative: str | None = "scores.json"
    else:
        score_sha256 = None
        score_relative = None
    initial_artifact = _snapshot_artifact(initial_snapshot, directory)
    final_artifact = _snapshot_artifact(final_snapshot, directory)
    snapshot_binding = (
        _derive_snapshot_binding(
            initial_snapshot,
            final_snapshot,
            agent_result,
            run_id=run_id,
        )
        if evaluation is not None
        else None
    )
    transcript_relative = _optional_relative_path(
        directory / "transcript.jsonl", directory
    )
    if transcript_relative is None:
        transcript_sha256 = None
    else:
        transcript_bytes = _read_verified_regular_file(
            directory / transcript_relative, "transcript artifact"
        )
        try:
            sanitized_transcript = sanitize_transcript_bytes(transcript_bytes)
        except SanitizationError as error:
            raise ResultArtifactError(
                "transcript artifact cannot be sanitized safely"
            ) from error
        if transcript_bytes != sanitized_transcript:
            raise ResultArtifactError(
                "transcript artifact is not canonical and sanitized"
            )
        transcript_sha256 = hashlib.sha256(sanitized_transcript).hexdigest()
    try:
        sanitized_agent_result = sanitize_for_persistence(agent_result)
    except SanitizationError as error:
        raise ResultArtifactError("agent result cannot be sanitized safely") from error
    if not isinstance(sanitized_agent_result, Mapping):  # pragma: no cover - defensive
        raise ResultArtifactError("agent result cannot be sanitized safely")
    stored_agent_result = _json_mapping(
        cast(Mapping[str, object], sanitized_agent_result)
    )
    stored_error = _safe_error(error) if error is not None else None
    artifact = RunArtifact(
        status=status,
        episode_id=episode_id,
        run_id=run_id,
        model=model,
        world_id=world_id,
        world_version=world_version,
        run_kind=run_kind,
        artifact_id=_artifact_id(
            episode_id,
            run_id,
            model,
            computed_configuration_hash,
            initial_artifact,
            final_artifact,
            snapshot_binding,
            sweep_id,
            sweep_run_index,
            fresh_world_id,
            score_sha256,
            transcript_sha256,
            stored_agent_result,
            status,
            stored_error,
        ),
        configuration=public_configuration,
        configuration_hash=computed_configuration_hash,
        sweep_id=sweep_id,
        sweep_run_index=sweep_run_index,
        fresh_world_id=fresh_world_id,
        judge_models=tuple(sorted(judge_models)),
        seed=seed,
        initial_snapshot=initial_artifact,
        final_snapshot=final_artifact,
        snapshot_binding=snapshot_binding,
        transcript_path=transcript_relative,
        transcript_sha256=transcript_sha256,
        agent_result=stored_agent_result,
        score_path=score_relative,
        score_sha256=score_sha256,
        error=stored_error,
    )
    write_json_atomic(
        directory / "run.json", artifact.model_dump(mode="json"), output_root=directory
    )
    return artifact


def load_run_artifact(path: str | Path) -> tuple[RunArtifact, EvaluationResult]:
    """Load only a complete, hash-verified run artifact and its E.17 score."""

    candidate = Path(path)
    directory = candidate if candidate.is_dir() else candidate.parent
    _ensure_regular_directory(directory, create=False)
    artifact_path = directory / "run.json"
    artifact = _parse_model(
        RunArtifact,
        _read_verified_regular_file(artifact_path, "run artifact"),
        artifact_path,
    )
    if (
        artifact.status != "complete"
        or artifact.score_path is None
        or artifact.score_sha256 is None
    ):
        raise ResultArtifactError(
            "run artifact is incomplete and cannot be evaluated or reported"
        )
    score_relative = _safe_relative_path(artifact.score_path)
    score_path = directory / score_relative
    _validate_run_artifact(artifact)
    score_bytes = _read_verified_regular_file(score_path, "scores artifact")
    if hashlib.sha256(score_bytes).hexdigest() != artifact.score_sha256:
        raise ResultArtifactError("scores artifact hash verification failed")
    result = _parse_model(EvaluationResult, score_bytes, score_path)
    if (
        result.episode_id != artifact.episode_id
        or result.run_id != artifact.run_id
        or result.model != artifact.model
    ):
        raise ResultArtifactError("scores artifact contradicts run provenance")
    _verify_snapshot(directory, artifact.initial_snapshot)
    _verify_snapshot(directory, artifact.final_snapshot)
    _verify_snapshot_binding(directory, artifact, result)
    if artifact.transcript_path is None:
        if artifact.transcript_sha256 is not None:
            raise ResultArtifactError("transcript artifact provenance is inconsistent")
    else:
        if artifact.transcript_sha256 is None:
            raise ResultArtifactError("transcript artifact hash is missing")
        transcript = _read_verified_regular_file(
            directory / _safe_relative_path(artifact.transcript_path),
            "transcript artifact",
        )
        try:
            sanitized_transcript = sanitize_transcript_bytes(transcript)
        except SanitizationError as error:
            raise ResultArtifactError(
                "transcript artifact cannot be sanitized safely"
            ) from error
        if transcript != sanitized_transcript:
            raise ResultArtifactError(
                "transcript artifact is not canonical and sanitized"
            )
        if (
            hashlib.sha256(sanitized_transcript).hexdigest()
            != artifact.transcript_sha256
        ):
            raise ResultArtifactError("transcript artifact hash verification failed")
    return artifact, result


def build_aggregate_artifact(
    runs: Sequence[tuple[RunArtifact, EvaluationResult]],
) -> AggregateArtifact:
    """Aggregate sorted, compatible native runs using WP-09 reliability logic."""

    # Imported lazily: evaluation persistence depends on this module's atomic writer,
    # while aggregation happens only after that import graph is fully initialized.
    from mirrorfirm.evaluation.scoring import reliability_summary

    if not runs:
        raise ResultArtifactError("at least one completed run is required")
    run_ids = [artifact.run_id for artifact, _ in runs]
    artifact_ids = [artifact.artifact_id for artifact, _ in runs]
    if len(set(run_ids)) != len(run_ids) or len(set(artifact_ids)) != len(artifact_ids):
        raise ResultArtifactError(
            "duplicate run or artifact identity cannot enter aggregate"
        )
    ordered = sorted(runs, key=lambda item: item[0].run_id)
    first_artifact, first_result = ordered[0]
    for artifact, result in ordered:
        if artifact.status != "complete":
            raise ResultArtifactError("incomplete runs cannot enter an aggregate")
        if (
            result.episode_id != artifact.episode_id
            or result.run_id != artifact.run_id
            or result.model != artifact.model
        ):
            raise ResultArtifactError("evaluation result contradicts run provenance")
        _validate_run_artifact(artifact)
        if (
            artifact.episode_id,
            artifact.model,
            artifact.world_id,
            artifact.world_version,
            artifact.configuration_hash,
            artifact.judge_models,
            artifact.run_kind,
        ) != (
            first_artifact.episode_id,
            first_artifact.model,
            first_artifact.world_id,
            first_artifact.world_version,
            first_artifact.configuration_hash,
            first_artifact.judge_models,
            first_artifact.run_kind,
        ):
            raise ResultArtifactError("run artifacts are not comparison-compatible")
        if artifact.configuration != first_artifact.configuration:
            raise ResultArtifactError("run artifacts have incompatible configuration")
    results = tuple(result for _, result in ordered)
    kind = first_artifact.run_kind
    return AggregateArtifact(
        kind=kind,
        label=(REFERENCE_DISPLAY_LABEL if kind == "scripted_reference" else None),
        episode_id=first_artifact.episode_id,
        model=first_artifact.model,
        world_id=first_artifact.world_id,
        world_version=first_artifact.world_version,
        run_count=len(ordered),
        configuration=first_artifact.configuration,
        configuration_hash=first_artifact.configuration_hash,
        comparison_identity=_comparison_identity(first_artifact.configuration),
        judge_models=first_artifact.judge_models,
        runs=tuple(artifact for artifact, _ in ordered),
        results=results,
        reliability=reliability_summary(results),
        layer_means=_mean_layer_scores(results),
        usage_means=_mean_usage(results),
        critical_failure_ids=tuple(
            sorted(
                {
                    failure.cf_id
                    for result in results
                    for failure in result.critical_failures
                }
            )
        ),
    )


def write_aggregate_artifact(
    aggregate: AggregateArtifact,
    destination: str | Path | None = None,
    *,
    secure_directory: SecureExecutionDirectory | None = None,
    filename: str = "aggregate.json",
) -> Path:
    """Persist an aggregate with the full verified run rows required by reporting."""

    if secure_directory is not None:
        if destination is not None:
            raise ValueError("secure aggregate publication does not accept a pathname")
        if Path(filename).name != filename or not filename.endswith(".json"):
            raise ResultArtifactError("aggregate output filename is unsafe")
        return write_json_atomic_to_directory(
            secure_directory, filename, aggregate.model_dump(mode="json")
        )
    if destination is None:
        raise ValueError("aggregate destination is required")
    path = Path(destination)
    if path.suffix != ".json":
        path = path / "aggregate.json"
    return write_json_atomic(path, aggregate.model_dump(mode="json"))


def load_aggregate_artifact(path: str | Path) -> AggregateArtifact:
    """Load a deterministic aggregate without rerunning models or episodes."""

    candidate = Path(path)
    source = candidate / "aggregate.json" if candidate.is_dir() else candidate
    aggregate = _parse_model(
        AggregateArtifact,
        _read_verified_regular_file(source, "aggregate artifact"),
        source,
    )
    reconstructed = build_aggregate_artifact(
        list(zip(aggregate.runs, aggregate.results, strict=True))
    )
    if canonical_json(reconstructed.model_dump(mode="json")) != canonical_json(
        aggregate.model_dump(mode="json")
    ):
        raise ResultArtifactError(
            "aggregate artifact is inconsistent with its run rows"
        )
    return aggregate


def write_external_apex_artifact(evaluation: object, destination: str | Path) -> Path:
    """Persist a static APEX result with its required external contamination label."""

    model_dump = getattr(evaluation, "model_dump", None)
    if not callable(model_dump):
        raise ResultArtifactError("external APEX evaluation must be a typed model")
    payload = _json_mapping(cast(Mapping[str, object], model_dump(mode="json")))
    revision = payload.get("source_revision")
    label = payload.get("report_label")
    contamination = payload.get("contamination")
    if not isinstance(revision, str) or not isinstance(label, str):
        raise ResultArtifactError("external APEX evaluation provenance is invalid")
    if contamination != "public-reference-answers":
        raise ResultArtifactError("external APEX evaluation contamination is invalid")
    artifact = ExternalApexArtifact(
        source_revision=revision,
        report_label=label,
        contamination="public-reference-answers",
        evaluation=payload,
    )
    return write_json_atomic(destination, artifact.model_dump(mode="json"))


def load_external_apex_artifact(path: str | Path) -> ExternalApexArtifact:
    """Load an external-only result that must not cross into native aggregation."""

    source = Path(path)
    return _parse_model(
        ExternalApexArtifact,
        _read_verified_regular_file(source, "external APEX artifact"),
        source,
    )


def _snapshot_artifact(snapshot: StateSnapshot, directory: Path) -> SnapshotArtifact:
    path = Path(snapshot.db_path)
    relative_path = _required_relative_path(path, directory)
    snapshot_bytes = _read_verified_regular_file(
        directory / relative_path, f"{snapshot.phase} snapshot"
    )
    return SnapshotArtifact(
        id=snapshot.id,
        phase=snapshot.phase,
        state_digest=snapshot.state_digest,
        relative_path=relative_path,
        sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
    )


def _required_relative_path(path: Path, root: Path) -> str:
    try:
        return _safe_relative_path(
            path.absolute().relative_to(root.absolute()).as_posix()
        )
    except ValueError as error:
        raise ResultArtifactError(
            "run artifact path must stay inside its run directory"
        ) from error


def _optional_relative_path(path: Path, root: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _required_relative_path(path, root)


def _safe_relative_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise ResultArtifactError("artifact paths must be safe relative paths")
    return candidate.as_posix()


def _ensure_regular_directory(path: Path, *, create: bool = True) -> None:
    """Reject symlinked or special ancestors while creating ordinary directories."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise ResultArtifactError("artifact directory is missing or unsafe")
            try:
                current.mkdir()
                metadata = os.lstat(current)
            except OSError as error:
                raise ResultArtifactError(
                    "artifact directory is missing or unsafe"
                ) from error
        except OSError as error:
            raise ResultArtifactError(
                "artifact directory is missing or unsafe"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            # macOS exposes the system temporary hierarchy through /var.  It is a
            # platform-owned alias, not a caller-selected output component; retain
            # support for pytest's normal temporary root while rejecting all other
            # links in artifact paths.
            if _is_macos_var_alias(current):
                continue
            raise ResultArtifactError("artifact directory is missing or unsafe")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ResultArtifactError("artifact directory is missing or unsafe")


def _is_macos_var_alias(path: Path) -> bool:
    """Recognize only the platform-owned /var → private/var temporary alias."""

    if path != Path("/var"):
        return False
    try:
        return os.readlink(path) == "private/var"
    except OSError:
        return False


def _open_regular_directory(path: Path) -> int:
    """Retain a no-follow descriptor for an already validated output directory."""

    _ensure_regular_directory(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("not a directory")
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ResultArtifactError("artifact directory is missing or unsafe") from error


def _require_contained_destination(destination: Path, root: Path) -> None:
    """Require a syntactically contained output without resolving symlinks."""

    try:
        destination.absolute().relative_to(root.absolute())
    except ValueError as error:
        raise ResultArtifactError(
            "artifact output escapes its declared root"
        ) from error


def _verify_snapshot(directory: Path, snapshot: SnapshotArtifact) -> None:
    relative = _safe_relative_path(snapshot.relative_path)
    data = _read_verified_regular_file(
        directory / relative, f"{snapshot.phase} snapshot"
    )
    if hashlib.sha256(data).hexdigest() != snapshot.sha256:
        raise ResultArtifactError(f"{snapshot.phase} snapshot hash verification failed")
    if len(snapshot.state_digest) != 64 or any(
        character not in "0123456789abcdef" for character in snapshot.state_digest
    ):
        raise ResultArtifactError(f"{snapshot.phase} snapshot digest is invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="mirrorfirm-verified-snapshot-"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        with SQLiteWorldView.open(temporary) as view:
            if view.state_digest() != snapshot.state_digest:
                raise ResultArtifactError(
                    f"{snapshot.phase} snapshot logical digest verification failed"
                )
    except (OSError, ValueError) as error:
        if isinstance(error, ResultArtifactError):
            raise
        raise ResultArtifactError(f"{snapshot.phase} snapshot is invalid") from error
    finally:
        temporary.unlink(missing_ok=True)


def _derive_snapshot_binding(
    initial: StateSnapshot,
    final: StateSnapshot,
    agent_result: Mapping[str, object],
    *,
    run_id: str,
) -> SnapshotExecutionBinding:
    """Bind runner snapshots to the pre-agent state and committed terminal Action."""

    if initial.episode_run_id != run_id or final.episode_run_id != run_id:
        raise ResultArtifactError("snapshot execution provenance contradicts run id")
    expected_initial_id = f"snp-{run_id}-initial"
    if initial.id != expected_initial_id:
        raise ResultArtifactError("initial snapshot execution identity is invalid")
    if initial.phase != "initial" or final.phase != "final":
        raise ResultArtifactError("snapshot execution roles are invalid")
    with SQLiteWorldView.open(initial.db_path) as initial_view:
        if any(_successful_finish_action(action) for action in initial_view.actions()):
            raise ResultArtifactError("initial snapshot is not a pre-agent world state")
    with SQLiteWorldView.open(final.db_path) as final_view:
        actions = final_view.actions()
    terminal_actions = [
        action for action in actions if _successful_finish_action(action)
    ]
    if len(terminal_actions) != 1 or not actions or actions[-1] != terminal_actions[0]:
        raise ResultArtifactError("final snapshot has no unique terminal Action")
    terminal = terminal_actions[0]
    terminal_snapshot_id = _finish_snapshot_id(terminal)
    if terminal_snapshot_id != final.id:
        raise ResultArtifactError("final snapshot identity contradicts terminal Action")
    _verify_agent_terminal_summary(agent_result, terminal)
    if initial.state_digest != final.state_digest and initial.id == final.id:
        raise ResultArtifactError(
            "distinct snapshot states require distinct identities"
        )
    return SnapshotExecutionBinding(
        episode_run_id=run_id,
        initial_snapshot_id=initial.id,
        initial_state_digest=initial.state_digest,
        final_snapshot_id=final.id,
        final_state_digest=final.state_digest,
        terminal_action_id=terminal.id,
        terminal_action_output_digest=terminal.output_digest,
    )


def _verify_snapshot_binding(
    directory: Path,
    artifact: RunArtifact,
    result: EvaluationResult,
) -> None:
    """Reconstruct semantic snapshot roles from retained snapshot bytes at load time."""

    binding = artifact.snapshot_binding
    if binding is None:
        raise ResultArtifactError(
            "complete run artifact lacks snapshot execution binding"
        )
    initial = artifact.initial_snapshot
    final = artifact.final_snapshot
    if (
        binding.episode_run_id != artifact.run_id
        or binding.initial_snapshot_id != initial.id
        or binding.initial_state_digest != initial.state_digest
        or binding.final_snapshot_id != final.id
        or binding.final_state_digest != final.state_digest
        or initial.phase != "initial"
        or final.phase != "final"
        or initial.id != f"snp-{artifact.run_id}-initial"
    ):
        raise ResultArtifactError("snapshot execution binding is inconsistent")
    if result.run_id != binding.episode_run_id:
        raise ResultArtifactError("evaluation result contradicts snapshot execution")
    initial_path = directory / _safe_relative_path(initial.relative_path)
    final_path = directory / _safe_relative_path(final.relative_path)
    with SQLiteWorldView.open(initial_path) as initial_view:
        if any(_successful_finish_action(action) for action in initial_view.actions()):
            raise ResultArtifactError("initial snapshot is not a pre-agent world state")
    with SQLiteWorldView.open(final_path) as final_view:
        actions = final_view.actions()
    terminal_actions = [
        action for action in actions if _successful_finish_action(action)
    ]
    if len(terminal_actions) != 1 or not actions or actions[-1] != terminal_actions[0]:
        raise ResultArtifactError("final snapshot has no unique terminal Action")
    terminal = terminal_actions[0]
    if (
        terminal.id != binding.terminal_action_id
        or terminal.output_digest != binding.terminal_action_output_digest
        or _finish_snapshot_id(terminal) != final.id
    ):
        raise ResultArtifactError("final snapshot contradicts terminal execution")
    _verify_agent_terminal_summary(artifact.agent_result, terminal)
    if initial.state_digest != final.state_digest and initial.id == final.id:
        raise ResultArtifactError(
            "distinct snapshot states require distinct identities"
        )


def _successful_finish_action(action: Action) -> bool:
    return action.tool == "finish_episode" and _finish_snapshot_id(action) is not None


def _finish_snapshot_id(action: Action) -> str | None:
    payload = action.output_payload
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("final_snapshot_id")
    return value if isinstance(value, str) and value else None


def _verify_agent_terminal_summary(
    agent_result: Mapping[str, object], terminal: Action
) -> None:
    """Require the persisted provider result to agree with the terminal tool result."""

    summary = agent_result.get("finish_summary")
    if not isinstance(summary, Mapping):
        raise ResultArtifactError("agent result lacks terminal completion provenance")
    output = terminal.output_payload
    if not isinstance(output, Mapping):  # guarded by _successful_finish_action
        raise ResultArtifactError("terminal Action output is malformed")
    for key in ("summary", "deliverable_refs", "unresolved_items", "final_snapshot_id"):
        if summary.get(key) != output.get(key):
            raise ResultArtifactError(
                "agent terminal completion contradicts terminal Action"
            )


def _artifact_id(
    episode_id: str,
    run_id: str,
    model: str,
    configuration_hash: str,
    initial: SnapshotArtifact,
    final: SnapshotArtifact,
    snapshot_binding: SnapshotExecutionBinding | None,
    sweep_id: str | None,
    sweep_run_index: int | None,
    fresh_world_id: str | None,
    score_sha256: str | None,
    transcript_sha256: str | None,
    agent_result: Mapping[str, JsonValue],
    status: str,
    error: str | None,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "episode_id": episode_id,
                "run_id": run_id,
                "model": model,
                "configuration_hash": configuration_hash,
                "initial_snapshot": initial.model_dump(mode="json"),
                "final_snapshot": final.model_dump(mode="json"),
                "snapshot_binding": (
                    snapshot_binding.model_dump(mode="json")
                    if snapshot_binding is not None
                    else None
                ),
                "sweep_id": sweep_id,
                "sweep_run_index": sweep_run_index,
                "fresh_world_id": fresh_world_id,
                "score_sha256": score_sha256,
                "transcript_sha256": transcript_sha256,
                "agent_result": agent_result,
                "status": status,
                "error": error,
            }
        ).encode("utf-8")
    ).hexdigest()


def _comparison_identity(configuration: PublicRunConfiguration) -> ComparisonIdentity:
    return ComparisonIdentity(
        suite_version=configuration.suite_version,
        scoring_version=configuration.scoring_version,
        episode_id=configuration.episode_id,
        world_id=configuration.world_id,
        world_version=configuration.world_version,
        jurisdiction=configuration.jurisdiction,
        run_kind=configuration.run_kind,
        judge_models=configuration.judge_models,
        seed=configuration.seed,
        temperature=configuration.temperature,
        max_steps=configuration.max_steps,
        max_tokens=configuration.max_tokens,
        max_world_days=configuration.max_world_days,
        baseline=configuration.baseline,
    )


def _validate_run_identity(model: str, configuration: PublicRunConfiguration) -> None:
    if configuration.run_kind == "scripted_reference":
        if (
            model != REFERENCE_DISPLAY_LABEL
            or configuration.model_identifier != "reference-scripted"
        ):
            raise ResultArtifactError(
                "scripted reference provenance has an invalid display identity"
            )
    elif (
        model in {"reference-scripted", REFERENCE_DISPLAY_LABEL}
        or configuration.model_identifier == "reference-scripted"
    ):
        raise ResultArtifactError(
            "reference provenance cannot masquerade as a native model"
        )


def _validate_run_artifact(artifact: RunArtifact) -> None:
    expected_hash = canonical_configuration_hash(
        artifact.configuration.model_dump(mode="json")
    )
    if artifact.configuration_hash != expected_hash:
        raise ResultArtifactError("configuration hash verification failed")
    if (
        artifact.configuration.episode_id != artifact.episode_id
        or artifact.configuration.world_id != artifact.world_id
        or artifact.configuration.world_version != artifact.world_version
        or artifact.configuration.judge_models != artifact.judge_models
        or artifact.configuration.seed != artifact.seed
        or artifact.configuration.run_kind != artifact.run_kind
    ):
        raise ResultArtifactError("public configuration contradicts run provenance")
    sweep_values = (
        artifact.sweep_id,
        artifact.sweep_run_index,
        artifact.fresh_world_id,
    )
    if any(value is None for value in sweep_values) and any(
        value is not None for value in sweep_values
    ):
        raise ResultArtifactError("sweep execution provenance is incomplete")
    if artifact.sweep_run_index is not None and artifact.sweep_run_index <= 0:
        raise ResultArtifactError("sweep execution run index is invalid")
    _validate_run_identity(artifact.model, artifact.configuration)
    expected_identity = _artifact_id(
        artifact.episode_id,
        artifact.run_id,
        artifact.model,
        artifact.configuration_hash,
        artifact.initial_snapshot,
        artifact.final_snapshot,
        artifact.snapshot_binding,
        artifact.sweep_id,
        artifact.sweep_run_index,
        artifact.fresh_world_id,
        artifact.score_sha256,
        artifact.transcript_sha256,
        artifact.agent_result,
        artifact.status,
        artifact.error,
    )
    if artifact.artifact_id != expected_identity:
        raise ResultArtifactError("run artifact identity verification failed")


def _public_configuration(
    configuration: Mapping[str, object],
    *,
    episode_id: str,
    model_identifier: str,
    world_id: str,
    world_version: str,
    judge_models: Sequence[str],
    seed: int | None,
    run_kind: RunKind,
    jurisdiction: str,
) -> PublicRunConfiguration:
    values = _public_mapping(configuration, reject_unknown=True)
    values.pop("model", None)
    values.pop("model_identifier", None)
    supplied = values.get("run_kind")
    if supplied is not None and supplied != run_kind:
        raise ResultArtifactError(
            "configuration run kind contradicts execution provenance"
        )
    return PublicRunConfiguration.model_validate(
        {
            **values,
            "episode_id": episode_id,
            "model_identifier": model_identifier,
            "world_id": world_id,
            "world_version": world_version,
            "jurisdiction": jurisdiction,
            "run_kind": run_kind,
            "judge_models": tuple(sorted(judge_models)),
            "seed": seed,
        }
    )


def _public_mapping(
    value: Mapping[str, object], *, reject_unknown: bool
) -> dict[str, object]:
    public: dict[str, object] = {}
    for key, item in value.items():
        key_text = str(key)
        if _is_secret_key(key_text):
            continue
        if key_text not in _PUBLIC_CONFIGURATION_FIELDS:
            if reject_unknown:
                raise ResultArtifactError(
                    "configuration contains an unsupported public field"
                )
            continue
        public[key_text] = item
    return public


def _read_verified_regular_file(path: Path, label: str) -> bytes:
    """Read one regular non-symlink artifact from a retained descriptor."""

    try:
        _ensure_regular_directory(path.parent, create=False)
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ResultArtifactError(f"{label} is missing or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResultArtifactError(f"{label} is missing or unsafe")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise ResultArtifactError(f"{label} is missing or unsafe") from error
    finally:
        os.close(descriptor)


def _parse_model[T: BaseModel](model: type[T], data: bytes, path: Path) -> T:
    try:
        return model.model_validate_json(data)
    except ValidationError as error:
        raise ResultArtifactError(f"{path} is invalid") from error


def _mean_layer_scores(results: Sequence[EvaluationResult]) -> LayerScores:
    count = len(results)
    return LayerScores(
        task_completion=sum(result.scores.task_completion for result in results)
        / count,
        accounting=sum(result.scores.accounting for result in results) / count,
        state=sum(result.scores.state for result in results) / count,
        safety=sum(result.scores.safety for result in results) / count,
        provenance=sum(result.scores.provenance for result in results) / count,
        communication=sum(result.scores.communication for result in results) / count,
        efficiency=sum(result.scores.efficiency for result in results) / count,
    )


def _mean_usage(results: Sequence[EvaluationResult]) -> Usage:
    count = len(results)
    return Usage(
        steps=round(sum(result.usage.steps for result in results) / count),
        tokens_in=round(sum(result.usage.tokens_in for result in results) / count),
        tokens_out=round(sum(result.usage.tokens_out for result in results) / count),
        cost_usd=sum(result.usage.cost_usd for result in results) / count,
        latency_s=sum(result.usage.latency_s for result in results) / count,
        world_days_elapsed=sum(result.usage.world_days_elapsed for result in results)
        / count,
    )


def _json_mapping(value: Mapping[str, object]) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        parsed = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ResultArtifactError(
            "artifact payload is not JSON-serializable"
        ) from error
    if not isinstance(
        parsed, dict
    ):  # pragma: no cover - mapping input makes this defensive
        raise ResultArtifactError("artifact payload must be a JSON object")
    return cast(dict[str, JsonValue], parsed)


def _canonical_evaluation_payload(result: EvaluationResult) -> dict[str, JsonValue]:
    """Sort unordered E.17 collections without adding a competing score schema."""

    payload = result.model_dump(mode="json")
    criteria = payload.get("criterion_results")
    if isinstance(criteria, list):
        for criterion in criteria:
            if isinstance(criterion, dict) and isinstance(
                criterion.get("evidence_refs"), list
            ):
                criterion["evidence_refs"] = sorted(criterion["evidence_refs"])
        payload["criterion_results"] = sorted(
            criteria,
            key=lambda item: (
                str(item.get("criterion_id", "")) if isinstance(item, dict) else ""
            ),
        )
    failures = payload.get("critical_failures")
    if isinstance(failures, list):
        for failure in failures:
            if isinstance(failure, dict) and isinstance(
                failure.get("evidence_refs"), list
            ):
                failure["evidence_refs"] = sorted(failure["evidence_refs"])
        payload["critical_failures"] = sorted(
            failures,
            key=lambda item: (
                (
                    str(item.get("cf_id", "")),
                    str(item.get("detail", "")),
                )
                if isinstance(item, dict)
                else ("", "")
            ),
        )
    return cast(dict[str, JsonValue], payload)


def _is_secret_key(value: str) -> bool:
    """Recognise credentials across normalized spelling variants, never values."""

    normalized = "".join(
        character for character in value.lower() if character.isalnum()
    )
    public_normalized = {
        "".join(character for character in key.lower() if character.isalnum())
        for key in _PUBLIC_CONFIGURATION_FIELDS
    }
    return normalized not in public_normalized and is_secret_key(value)


def _safe_error(value: str) -> str:
    """Do not persist exception strings that happen to contain credential-like text."""

    sanitized = sanitize_text(value)
    return (
        "execution failed without a persisted provider error"
        if sanitized != value
        else sanitized
    )
