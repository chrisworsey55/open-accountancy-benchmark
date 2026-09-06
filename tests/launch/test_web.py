"""Launch-route, accessibility, and registration persistence coverage."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest

from mirrorfirm.cli import main as cli_main
from mirrorfirm.launch.web import LaunchApplication


def _request(
    app: LaunchApplication,
    method: str,
    path: str,
    data: dict[str, str] | None = None,
) -> tuple[str, dict[str, str], str]:
    body = urlencode(data or {}).encode("utf-8")
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    response = app(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        },
        start_response,
    )
    return (
        str(captured["status"]),
        dict(captured["headers"]),
        b"".join(response).decode("utf-8"),
    )


def _developer_data() -> dict[str, str]:
    return {
        "name": "Avery Example",
        "email": "avery@example.test",
        "profile_url": "https://github.com/avery-example",
        "agent_name": "fictional-agent",
        "organisation": "Example Labs",
        "jurisdictions": "both",
        "workflows": "Classification and reconciliation",
        "benchmark_run": "no",
        "message": "Please review this fictional submission.",
        "consent": "yes",
        "website": "",
    }


def _workflow_data() -> dict[str, str]:
    return {
        "contact_name": "Morgan Example",
        "email": "morgan@example.test",
        "firm_name": "Example Accountancy LLP",
        "country": "United Kingdom",
        "jurisdiction": "uk",
        "workflow_title": "Month-end evidence chase",
        "current_process": "A fictional manual chase process.",
        "bottleneck": "Missing evidence.",
        "evidence_requirements": "Receipts and approvals.",
        "approval_requirements": "Reviewer sign-off.",
        "monthly_volume": "10",
        "accounting_software": "Fictional Books",
        "design_partner": "yes",
        "notes": "",
        "consent": "yes",
        "website": "",
    }


def test_homepage_has_agreed_copy_actions_metadata_and_accessibility_landmarks() -> (
    None
):
    status, headers, body = _request(LaunchApplication(), "GET", "/")

    assert status == "200 OK"
    assert "Franklin &amp; McGrath" in body
    assert "Where AI agents prove they can do real accounting work." in body
    assert "View the Agent League Table" in body
    assert 'meta property="og:title"' in body
    assert 'href="#content"' in body
    assert "Mirror Firm" not in body
    assert "Content-Security-Policy" in headers


def test_empty_leaderboard_is_truthful_and_supports_filters() -> None:
    app = LaunchApplication()
    status, _, body = _request(app, "GET", "/leaderboard")

    assert status == "200 OK"
    assert "Franklin &amp; McGrath Agent League Table" in body
    assert "The first external agent results have not yet been published" in body
    assert "jurisdiction=uk" in body
    assert "jurisdiction=us" in body
    assert "No published reference artifacts are configured" in body


def test_developer_registration_persists_and_duplicate_is_truthful(
    tmp_path: Path,
) -> None:
    database = tmp_path / "leads.sqlite3"
    app = LaunchApplication(leads_database=database)

    first_status, _, first_body = _request(
        app, "POST", "/register/developer", _developer_data()
    )
    second_status, _, second_body = _request(
        app, "POST", "/register/developer", _developer_data()
    )

    assert first_status == "200 OK"
    assert "has been recorded" in first_body
    assert second_status == "200 OK"
    assert "already have this registration" in second_body
    assert database.is_file()


def test_workflow_registration_persists_without_public_exposure(tmp_path: Path) -> None:
    database = tmp_path / "leads.sqlite3"
    app = LaunchApplication(leads_database=database)

    status, _, body = _request(app, "POST", "/register/workflow", _workflow_data())

    assert status == "200 OK"
    assert "has been recorded" in body
    assert "Example Accountancy LLP" not in _request(app, "GET", "/")[2]


def test_validation_spam_and_storage_failures_do_not_claim_success(
    tmp_path: Path,
) -> None:
    app = LaunchApplication(leads_database=tmp_path / "leads.sqlite3")
    invalid = _developer_data() | {"profile_url": "not-a-url"}
    spam = _developer_data() | {"website": "https://spam.example"}
    unavailable = LaunchApplication(leads_database=tmp_path)

    assert _request(app, "POST", "/register/developer", invalid)[0] == "400 Bad Request"
    assert _request(app, "POST", "/register/developer", spam)[0] == "400 Bad Request"
    status, _, body = _request(
        unavailable, "POST", "/register/workflow", _workflow_data()
    )
    assert status == "503 Service Unavailable"
    assert "No submission was recorded" in body


def test_missing_storage_and_unknown_routes_have_honest_error_states() -> None:
    app = LaunchApplication()

    status, _, body = _request(app, "POST", "/register/developer", _developer_data())
    assert status == "503 Service Unavailable"
    assert "No submission was recorded" in body
    assert _request(app, "GET", "/missing")[0] == "404 Not Found"


def test_launch_cli_rejects_an_invalid_port_without_starting_a_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_main(["launch", "serve", "--port", "0"]) == 2
    captured = capsys.readouterr()
    assert "port must be between 1 and 65535" in captured.err
