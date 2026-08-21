"""Static, no-network release checklist for the complete source candidate.

The gate deliberately examines all non-ignored candidate files, rather than just the
Git index: a release build can contain staged, unstaged, and untracked files.  It never
follows a candidate symlink, so an apparent in-tree source cannot cause us to inspect a
file outside the repository.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "results",
    }
)
_IGNORED_FILES = frozenset({".coverage", ".DS_Store"})
_CREDENTIAL_CANARY = "CANARY_" + "PRIVATE_KEY"
_PRIVATE_MACHINE_PATH = "/" + "Users/"


class ReleaseCandidateError(RuntimeError):
    """A candidate path cannot be safely inspected without leaving the repository."""


def _tracked_paths() -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return tuple(path for path in completed.stdout.splitlines() if path)


def _candidate_root(root: Path) -> Path:
    """Return a non-symlink directory root without resolving an attacker path."""

    try:
        metadata = os.lstat(root)
    except OSError as error:
        raise ReleaseCandidateError(f"release root is unreadable: {root}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseCandidateError("release root must be a real directory")
    return root.absolute()


def _is_ignored_directory(name: str) -> bool:
    return name in _IGNORED_DIRECTORIES or name.endswith(".egg-info")


def release_candidate_paths(root: Path | None = None) -> tuple[Path, ...]:
    """Enumerate every non-generated candidate path without following symlinks."""

    candidate_root = _candidate_root(ROOT if root is None else root)
    paths: list[Path] = []

    def walk(directory: Path) -> Iterator[Path]:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise ReleaseCandidateError(
                f"release candidate directory is unreadable: "
                f"{directory.relative_to(candidate_root).as_posix()}"
            ) from error
        for entry in entries:
            path = directory / entry.name
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ReleaseCandidateError(
                    "release candidate path is unreadable: "
                    f"{path.relative_to(candidate_root).as_posix()}"
                ) from error
            if stat.S_ISDIR(metadata.st_mode):
                if not _is_ignored_directory(entry.name):
                    yield from walk(path)
                continue
            if entry.name not in _IGNORED_FILES:
                yield path

    paths.extend(walk(candidate_root))
    return tuple(paths)


def _read_regular_file(path: Path) -> bytes:
    """Read a candidate file once from a no-follow regular-file descriptor."""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ReleaseCandidateError(f"unsafe release candidate path: {path}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseCandidateError(
                f"release candidate is not a regular file: {path}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65_536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _candidate_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _source_policy_errors(relative_path: str, contents: bytes) -> tuple[str, ...]:
    """Apply the existing source policy to UTF-8 candidate content.

    Candidate status is irrelevant: tracked, staged, unstaged, and untracked source is
    subject to the same release policy.  A NUL byte or invalid UTF-8 identifies a
    binary asset, which remains eligible after the regular-file and containment checks;
    its filename never decides whether content is scanned.
    """

    if b"\0" in contents:
        return ()
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    if _PRIVATE_MACHINE_PATH in text or _CREDENTIAL_CANARY in text:
        return (
            "release source has a developer path or credential canary: "
            f"{relative_path}",
        )
    return ()


def _read_required_text(root: Path, relative_path: str) -> str:
    """Read a required text file through the same candidate security boundary."""

    path = root / relative_path
    try:
        return _read_regular_file(path).decode("utf-8")
    except (ReleaseCandidateError, UnicodeDecodeError) as error:
        raise ReleaseCandidateError(
            f"required release file is unsafe or invalid: {relative_path}"
        ) from error


def release_errors(root: Path | None = None) -> tuple[str, ...]:
    """Check the complete source candidate without claiming runtime guarantees."""

    errors: list[str] = []
    candidate_root = _candidate_root(ROOT if root is None else root)
    try:
        candidate_paths = release_candidate_paths(candidate_root)
    except ReleaseCandidateError as error:
        return (str(error),)

    for source in candidate_paths:
        relative_path = _candidate_relative(source, candidate_root)
        try:
            metadata = os.lstat(source)
        except OSError:
            errors.append(f"release candidate path is unreadable: {relative_path}")
            continue
        if stat.S_ISLNK(metadata.st_mode):
            errors.append(f"release candidate symlink is unsafe: {relative_path}")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            errors.append(f"release candidate is not a regular file: {relative_path}")
            continue
        if source.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            errors.append(
                f"compiled database artifact is in release candidate: {relative_path}"
            )
        try:
            errors.extend(
                _source_policy_errors(relative_path, _read_regular_file(source))
            )
        except ReleaseCandidateError:
            errors.append(f"release candidate is unsafe or unreadable: {relative_path}")

    try:
        tracked = _tracked_paths()
    except subprocess.SubprocessError as error:
        errors.append(f"could not inspect tracked release paths: {error}")
        tracked = ()
    if any(path == "results" or path.startswith("results/") for path in tracked):
        errors.append("generated result artifacts must not be tracked")
    if any(path.startswith("dist/") or path.startswith("build/") for path in tracked):
        errors.append("build artifacts must not be tracked")
    for relative_path in tracked:
        if relative_path.endswith((".db", ".sqlite", ".sqlite3")):
            errors.append("compiled database artifacts must not be tracked")
            break

    try:
        pyproject = _read_required_text(candidate_root, "pyproject.toml")
        gitignore = _read_required_text(candidate_root, ".gitignore")
    except ReleaseCandidateError as error:
        errors.append(str(error))
        return tuple(errors)
    for required in (
        '"worlds" = "worlds"',
        '"episodes" = "episodes"',
        '"schemas" = "schemas"',
        '"LICENSES/harvey-labs.MIT.txt" = "LICENSES/harvey-labs.MIT.txt"',
    ):
        if required not in pyproject:
            errors.append(f"wheel force-include is missing {required}")
    if "results/" not in gitignore:
        errors.append("results directory is not gitignored")
    return tuple(errors)


def main() -> int:
    """Execute the source-only release checklist."""

    errors = release_errors()
    if errors:
        print("release check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "release check passed: complete source candidate contains no release artifacts"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - command entry point
    raise SystemExit(main())
