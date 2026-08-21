"""WP-14 release checks for docs, packaging declarations, and the offline demo."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from mirrorfirm.reporting import load_run_artifact

ROOT = Path(__file__).resolve().parents[2]
_CREDENTIAL_CANARY = "CANARY_" + "PRIVATE_KEY"
_PRIVATE_MACHINE_PATH = "/" + "Users/chrisworsey/"
_BINARY_PNG = b"\x89PNG\r\n\x1a\n\x00synthetic-binary-asset"


def _load_script(name: str) -> ModuleType:
    source = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise AssertionError(f"could not load {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_demo(output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/run_demo.py", "--output-dir", str(output)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )


def _candidate_root(path: Path) -> Path:
    """Create the smallest release-candidate layout for scanner regressions."""

    path.mkdir()
    path.joinpath("pyproject.toml").write_text(
        "\n".join(
            (
                "[tool.hatch.build.targets.wheel.force-include]",
                '"worlds" = "worlds"',
                '"episodes" = "episodes"',
                '"jurisdictions" = "jurisdictions"',
                '"schemas" = "schemas"',
                '"LICENSES/harvey-labs.MIT.txt" = "LICENSES/harvey-labs.MIT.txt"',
                "",
            )
        ),
        encoding="utf-8",
    )
    path.joinpath(".gitignore").write_text("results/\n", encoding="utf-8")
    path.joinpath("README.md").write_text(
        "synthetic release candidate\n", encoding="utf-8"
    )
    return path


def _release_errors_in_candidate(
    release: ModuleType, monkeypatch: object, candidate: Path
) -> tuple[str, ...]:
    """Run the release gate against an isolated candidate without Git state."""

    set_attribute = getattr(monkeypatch, "setattr")
    set_attribute(release, "ROOT", candidate)
    set_attribute(release, "_tracked_paths", lambda: ())
    return release.release_errors()


def _repository_copy(tmp_path: Path) -> Path:
    """Create an isolated working candidate for release-command boundary tests."""

    candidate = tmp_path / "release-candidate"
    shutil.copytree(
        ROOT,
        candidate,
        ignore=shutil.ignore_patterns(
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
            "*.egg-info",
        ),
    )
    initialized = subprocess.run(
        ["git", "init", "-q"],
        cwd=candidate,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr
    return candidate


def _run_release_check(candidate: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the public release command in an isolated candidate tree."""

    return subprocess.run(
        ["make", "release-check"],
        cwd=candidate,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )


def _stable_evaluation_payload(path: Path) -> dict[str, object]:
    """Keep runtime-measured latency out of a deterministic reference assertion."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage.pop("latency_s")
    return payload


def test_release_documentation_gate_and_documented_commands_are_current() -> None:
    """The no-network documentation build rejects stale release guidance."""

    docs = _load_script("check_docs")
    assert docs.check_docs() == ()
    completed = subprocess.run(
        [sys.executable, "scripts/check_docs.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert "documentation check passed" in completed.stdout

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "make demo",
        "mirror-firm list worlds",
        "mirror-firm validate --world",
        "mirror-firm run",
        "mirror-firm report",
        "mirror-firm compare",
        "mirror-firm sweep",
    ):
        assert command in readme


def test_release_source_check_and_wheel_resource_declarations() -> None:
    """Release sources declare runtime assets and exclude generated output trees."""

    release = _load_script("check_release")
    assert release.release_errors() == ()

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    includes = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert includes["worlds"] == "worlds"
    assert includes["episodes"] == "episodes"
    assert includes["jurisdictions"] == "jurisdictions"
    assert includes["schemas"] == "schemas"
    assert includes["LICENSES/harvey-labs.MIT.txt"] == "LICENSES/harvey-labs.MIT.txt"
    tracked = subprocess.run(
        ["git", "ls-files", "results"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert tracked.stdout == ""


def test_built_distributions_contain_runtime_resources_not_run_artifacts(
    tmp_path: Path,
) -> None:
    """The installed CLI has its authored resources without shipping generated runs."""

    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    wheel = next(tmp_path.glob("*.whl"))
    source = next(tmp_path.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_paths = archive.namelist()
    with tarfile.open(source, "r:gz") as archive:
        source_paths = archive.getnames()

    for path in (
        "mirrorfirm/cli.py",
        "worlds/uk-wyrley-brook/world.yaml",
        "episodes/uk/ep-uk-01.yaml",
        "jurisdictions/uk/pack.json",
        "schemas/Action.json",
    ):
        assert path in wheel_paths
        assert any(source_path.endswith(path) for source_path in source_paths)
    assert not any(path.startswith("results/") for path in wheel_paths)
    assert not any("/results/" in path for path in source_paths)


def test_built_distributions_retain_the_verbatim_harvey_licence(
    tmp_path: Path,
) -> None:
    """Every distributable copy must retain the licence named by NOTICE."""

    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    expected = ROOT / "LICENSES" / "harvey-labs.MIT.txt"
    relative = "LICENSES/harvey-labs.MIT.txt"
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as archive:
        assert relative in archive.namelist()
        assert archive.read(relative) == expected.read_bytes()
    with tarfile.open(next(tmp_path.glob("*.tar.gz")), "r:gz") as archive:
        source_path = next(
            name for name in archive.getnames() if name.endswith(relative)
        )
        extracted = archive.extractfile(source_path)
        assert extracted is not None
        assert extracted.read() == expected.read_bytes()


def test_clean_wheel_install_retains_harvey_licence_and_notice(tmp_path: Path) -> None:
    """Installed distributions retain the material referenced by the attribution notice."""

    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    environment = tmp_path / "installed"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert created.returncode == 0, created.stdout + created.stderr
    python = environment / "bin" / "python"
    installed = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(next(tmp_path.glob("*.whl"))),
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    check = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import sysconfig; from pathlib import Path; "
                "root=Path(sysconfig.get_paths()['purelib']); "
                "licence=root/'LICENSES'/'harvey-labs.MIT.txt'; "
                "notice=next(root.glob('mirror_firm-*.dist-info/licenses/NOTICE')); "
                "assert licence.read_bytes() == Path('LICENSES/harvey-labs.MIT.txt').read_bytes(); "
                "assert 'harvey-labs.MIT.txt' in notice.read_text(encoding='utf-8')"
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_release_check_rejects_untracked_docs_paths_and_scripts(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Candidate files cannot evade scanning by remaining untracked or scripted."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    candidate.joinpath("docs").mkdir()
    candidate.joinpath("scripts").mkdir()
    candidate.joinpath("docs", "secret.md").write_text(
        f"{_CREDENTIAL_CANARY}_RELEASE_DOC\n", encoding="utf-8"
    )
    candidate.joinpath("docs", "path.md").write_text(
        f"{_PRIVATE_MACHINE_PATH}private\n", encoding="utf-8"
    )
    candidate.joinpath("scripts", "release_hook.py").write_text(
        f"{_CREDENTIAL_CANARY}_RELEASE_SCRIPT\n", encoding="utf-8"
    )

    errors = _release_errors_in_candidate(release, monkeypatch, candidate)

    assert any("docs/secret.md" in error for error in errors)
    assert any("docs/path.md" in error for error in errors)
    assert any("scripts/release_hook.py" in error for error in errors)


def test_release_check_scans_utf8_text_regardless_of_filename(
    tmp_path: Path, monkeypatch: object
) -> None:
    """No suffix, misleading suffix, or Unicode name can suppress text scanning."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    files = {
        "docs/release-evidence": f"{_CREDENTIAL_CANARY}_EXTENSIONLESS\n",
        "docs/credential.rst": f"{_PRIVATE_MACHINE_PATH}private\n",
        "docs/claim.evidence": f"{_CREDENTIAL_CANARY}_UNFAMILIAR\n",
        "docs/misleading.png": f"{_CREDENTIAL_CANARY}_MISLEADING\n",
        "docs/über release evidence": f"{_CREDENTIAL_CANARY}_UNICODE\n",
        "scripts/release-input": f"{_CREDENTIAL_CANARY}_SCRIPT\n",
    }
    for relative_path, content in files.items():
        target = candidate / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    errors = _release_errors_in_candidate(release, monkeypatch, candidate)

    for relative_path in files:
        assert any(relative_path in error for error in errors), errors


def test_release_check_rejects_extensionless_source_before_it_enters_sdist(
    tmp_path: Path,
) -> None:
    """An untracked text file cannot pass release checking before sdist inclusion."""

    candidate = _repository_copy(tmp_path)
    attack = candidate / "docs" / "release-evidence"
    attack.write_text(f"{_CREDENTIAL_CANARY}_SDIST\n", encoding="utf-8")

    checked = _run_release_check(candidate)

    assert checked.returncode != 0
    assert "docs/release-evidence" in checked.stderr
    output = tmp_path / "artifacts"
    built = subprocess.run(
        ["uv", "build", "--out-dir", str(output)],
        cwd=candidate,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with tarfile.open(next(output.glob("*.tar.gz")), "r:gz") as archive:
        assert any(
            name.endswith("docs/release-evidence") for name in archive.getnames()
        )


def test_release_check_accepts_safe_binary_and_clean_extensionless_content(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Binary handling is byte-based while clean UTF-8 remains a valid source file."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    candidate.joinpath("docs").mkdir()
    candidate.joinpath("docs", "RELEASE-NOTES").write_text(
        "Fictional accounting evaluation release notes.\n", encoding="utf-8"
    )
    candidate.joinpath("docs", "packaged-image.bin").write_bytes(_BINARY_PNG)

    assert _release_errors_in_candidate(release, monkeypatch, candidate) == ()


def test_release_check_accepts_a_genuine_binary_asset_in_the_sdist(
    tmp_path: Path,
) -> None:
    """A binary asset remains a valid package input because its bytes are non-text."""

    candidate = _repository_copy(tmp_path)
    asset = candidate / "docs" / "synthetic-logo.bin"
    asset.write_bytes(_BINARY_PNG)

    checked = _run_release_check(candidate)

    assert checked.returncode == 0, checked.stdout + checked.stderr
    output = tmp_path / "artifacts"
    built = subprocess.run(
        ["uv", "build", "--out-dir", str(output)],
        cwd=candidate,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with tarfile.open(next(output.glob("*.tar.gz")), "r:gz") as archive:
        assert any(
            name.endswith("docs/synthetic-logo.bin") for name in archive.getnames()
        )


def test_release_check_does_not_treat_a_binary_suffix_as_binary_content(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Renaming UTF-8 source to a binary-looking suffix cannot evade the policy."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    candidate.joinpath("docs").mkdir()
    candidate.joinpath("docs", "not-really-an-image.png").write_text(
        f"{_CREDENTIAL_CANARY}_RENAMED\n", encoding="utf-8"
    )

    errors = _release_errors_in_candidate(release, monkeypatch, candidate)

    assert any("docs/not-really-an-image.png" in error for error in errors)


def test_release_check_rejects_dangling_and_special_candidate_paths(
    tmp_path: Path, monkeypatch: object
) -> None:
    """The no-follow traversal still rejects dangling and non-regular candidates."""

    temporary_root = Path(tempfile.mkdtemp(prefix="mf-"))
    try:
        release = _load_script("check_release")
        candidate = _candidate_root(temporary_root / "candidate")
        candidate.joinpath("docs").mkdir()
        candidate.joinpath("docs", "dangling").symlink_to(temporary_root / "missing")
        fifo = candidate / "docs" / "stream"
        os.mkfifo(fifo)
        socket_path = candidate / "docs" / "socket"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(socket_path))
            errors = _release_errors_in_candidate(release, monkeypatch, candidate)
    finally:
        shutil.rmtree(temporary_root)

    assert any("docs/dangling" in error and "symlink" in error for error in errors)
    assert any("docs/stream" in error and "regular file" in error for error in errors)
    assert any("docs/socket" in error and "regular file" in error for error in errors)


def test_release_check_rejects_a_symlinked_candidate_root(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A root path cannot enter a different repository through an ancestor link."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    linked = tmp_path / "linked-candidate"
    linked.symlink_to(candidate, target_is_directory=True)
    set_attribute = getattr(monkeypatch, "setattr")
    set_attribute(release, "ROOT", linked)

    with pytest.raises(release.ReleaseCandidateError, match="real directory"):
        release.release_errors()


def test_release_check_rejects_candidate_symlink_escape(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A release candidate may not follow an apparent file outside its root."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    outside = tmp_path / "outside.md"
    outside.write_text("synthetic outside file\n", encoding="utf-8")
    candidate.joinpath("docs").mkdir()
    candidate.joinpath("docs", "escape.md").symlink_to(outside)

    errors = _release_errors_in_candidate(release, monkeypatch, candidate)

    assert any("docs/escape.md" in error and "symlink" in error for error in errors)


def test_release_check_ignores_declared_generated_output(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Clearly generated caches do not make a clean candidate fail release checks."""

    release = _load_script("check_release")
    candidate = _candidate_root(tmp_path / "candidate")
    for directory in ("build", "dist", ".venv", ".git", "results", "__pycache__"):
        target = candidate / directory
        target.mkdir()
        target.joinpath(f"{_CREDENTIAL_CANARY}_IGNORED.txt").write_text(
            f"{_CREDENTIAL_CANARY}_IGNORED\n", encoding="utf-8"
        )

    assert _release_errors_in_candidate(release, monkeypatch, candidate) == ()


def test_release_check_includes_current_wp14_candidate_files() -> None:
    """The gate must examine the actual docs and scripts before it reports success."""

    release = _load_script("check_release")

    candidates = {
        path.relative_to(ROOT).as_posix()
        for path in release.release_candidate_paths(ROOT)
    }
    assert {
        "docs/architecture.md",
        "scripts/check_release.py",
        "scripts/run_demo.py",
    }.issubset(candidates)
    assert release.release_errors() == ()


def test_offline_demo_runs_the_real_cli_and_produces_verified_artifacts(
    tmp_path: Path,
) -> None:
    """A scripted reference exercises harness, scoring, aggregation, and reporting."""

    output = tmp_path / "reference-demo"
    completed = _run_demo(output)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "reference (scripted) - not model performance" in completed.stdout
    assert "overall=1.000; all_pass=True; critical_failures=0" in completed.stdout

    run_directory = (
        output / "runs" / "epi-uk-01" / "reference-scripted" / "run-demo-reference"
    )
    artifact, evaluation = load_run_artifact(run_directory)
    assert artifact.model == "reference (scripted) - not model performance"
    assert evaluation.overall == 1.0
    assert evaluation.all_pass is True
    assert evaluation.critical_failures == []
    assert len(tuple((output / "aggregates").rglob("*.json"))) == 1
    assert output.joinpath("reference-scorecard.html").is_file()
    assert output.joinpath("reference-scorecard.json").is_file()

    persisted_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in output.rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl", ".html", ".txt"}
    )
    assert "gold_output" not in persisted_text
    assert "answer-keys" not in persisted_text
    assert str(ROOT) not in persisted_text


def test_offline_demo_is_deterministic_for_fresh_output_directories(
    tmp_path: Path,
) -> None:
    """Fresh reference demo outputs keep score and aggregate bytes deterministic."""

    first = tmp_path / "first"
    second = tmp_path / "second"
    assert _run_demo(first).returncode == 0
    assert _run_demo(second).returncode == 0

    relative = Path("runs/epi-uk-01/reference-scripted/run-demo-reference/scores.json")
    assert _stable_evaluation_payload(first / relative) == _stable_evaluation_payload(
        second / relative
    )
    first_artifact, first_evaluation = load_run_artifact(
        first / "runs/epi-uk-01/reference-scripted/run-demo-reference"
    )
    second_artifact, second_evaluation = load_run_artifact(
        second / "runs/epi-uk-01/reference-scripted/run-demo-reference"
    )
    assert first_artifact.initial_snapshot.state_digest == (
        second_artifact.initial_snapshot.state_digest
    )
    assert first_artifact.final_snapshot.state_digest == (
        second_artifact.final_snapshot.state_digest
    )
    first_payload = first_evaluation.model_dump(mode="json")
    second_payload = second_evaluation.model_dump(mode="json")
    first_payload["usage"].pop("latency_s")
    second_payload["usage"].pop("latency_s")
    assert first_payload == second_payload
    assert "Pass^3" in first.joinpath("reference-scorecard.html").read_text(
        encoding="utf-8"
    )
