"""Minimal, server-validated local lead capture for the launch journeys."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


class LeadValidationError(ValueError):
    """A public form field was absent, malformed, or rejected as spam."""


class LeadStorageError(RuntimeError):
    """The configured durable registration store could not be used."""


@dataclass(frozen=True)
class SubmissionReceipt:
    """Truthful persistence result returned by either public registration path."""

    status: str
    message: str


_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_JURISDICTIONS = frozenset({"uk", "us", "both"})


class LeadStore:
    """A small SQLite boundary kept separate from benchmark-world storage."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def register_developer(self, data: Mapping[str, str]) -> SubmissionReceipt:
        values = _developer_values(data)
        return self._insert(
            """
            INSERT INTO developer_registrations (
                name, email, profile_url, agent_name, organisation, jurisdictions,
                workflows, benchmark_run, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
            "developer registration",
        )

    def register_workflow(self, data: Mapping[str, str]) -> SubmissionReceipt:
        values = _workflow_values(data)
        return self._insert(
            """
            INSERT INTO workflow_registrations (
                contact_name, email, firm_name, country, jurisdiction, workflow_title,
                current_process, bottleneck, evidence_requirements, approval_requirements,
                monthly_volume, accounting_software, design_partner, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
            "workflow registration",
        )

    def _insert(
        self, query: str, values: tuple[str, ...], label: str
    ) -> SubmissionReceipt:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.database_path) as connection:
                self._initialize(connection)
                try:
                    connection.execute(query, values)
                except sqlite3.IntegrityError:
                    return SubmissionReceipt(
                        "duplicate",
                        "We already have this registration and will review it.",
                    )
            os.chmod(self.database_path, 0o600)
        except (OSError, sqlite3.Error) as error:
            raise LeadStorageError(f"could not persist {label}") from error
        return SubmissionReceipt(
            "saved",
            "Thank you. Your registration has been recorded for manual review.",
        )

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS developer_registrations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                profile_url TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                organisation TEXT NOT NULL,
                jurisdictions TEXT NOT NULL,
                workflows TEXT NOT NULL,
                benchmark_run TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(email, agent_name)
            );
            CREATE TABLE IF NOT EXISTS workflow_registrations (
                id INTEGER PRIMARY KEY,
                contact_name TEXT NOT NULL,
                email TEXT NOT NULL,
                firm_name TEXT NOT NULL,
                country TEXT NOT NULL,
                jurisdiction TEXT NOT NULL,
                workflow_title TEXT NOT NULL,
                current_process TEXT NOT NULL,
                bottleneck TEXT NOT NULL,
                evidence_requirements TEXT NOT NULL,
                approval_requirements TEXT NOT NULL,
                monthly_volume TEXT NOT NULL,
                accounting_software TEXT NOT NULL,
                design_partner TEXT NOT NULL,
                notes TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(email, firm_name, workflow_title)
            );
            """
        )


def _developer_values(data: Mapping[str, str]) -> tuple[str, ...]:
    _reject_honeypot(data)
    jurisdiction = _required(data, "jurisdictions").casefold()
    if jurisdiction not in _JURISDICTIONS:
        raise LeadValidationError("choose UK, US, or both")
    benchmark_run = _required(data, "benchmark_run").casefold()
    if benchmark_run not in {"yes", "no"}:
        raise LeadValidationError("say whether you have run the benchmark")
    return (
        _required(data, "name"),
        _email(data),
        _url(data, "profile_url"),
        _required(data, "agent_name"),
        _optional(data, "organisation"),
        jurisdiction,
        _required(data, "workflows"),
        benchmark_run,
        _required(data, "message"),
    )


def _workflow_values(data: Mapping[str, str]) -> tuple[str, ...]:
    _reject_honeypot(data)
    jurisdiction = _required(data, "jurisdiction").casefold()
    if jurisdiction not in {"uk", "us"}:
        raise LeadValidationError("choose a UK or US jurisdiction")
    design_partner = _required(data, "design_partner").casefold()
    if design_partner not in {"yes", "no"}:
        raise LeadValidationError(
            "say whether you are interested in becoming a design partner"
        )
    return (
        _required(data, "contact_name"),
        _email(data),
        _required(data, "firm_name"),
        _required(data, "country"),
        jurisdiction,
        _required(data, "workflow_title"),
        _required(data, "current_process"),
        _required(data, "bottleneck"),
        _required(data, "evidence_requirements"),
        _required(data, "approval_requirements"),
        _optional(data, "monthly_volume"),
        _optional(data, "accounting_software"),
        design_partner,
        _optional(data, "notes"),
    )


def _required(data: Mapping[str, str], key: str) -> str:
    value = data.get(key, "").strip()
    if not value:
        raise LeadValidationError(f"{key.replace('_', ' ')} is required")
    if len(value) > 4_000:
        raise LeadValidationError(f"{key.replace('_', ' ')} is too long")
    return value


def _optional(data: Mapping[str, str], key: str) -> str:
    value = data.get(key, "").strip()
    if len(value) > 4_000:
        raise LeadValidationError(f"{key.replace('_', ' ')} is too long")
    return value


def _email(data: Mapping[str, str]) -> str:
    email = _required(data, "email").casefold()
    if not _EMAIL.fullmatch(email):
        raise LeadValidationError("enter a valid work email address")
    return email


def _url(data: Mapping[str, str], key: str) -> str:
    value = _required(data, key)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LeadValidationError("enter a valid GitHub profile or company URL")
    return value


def _reject_honeypot(data: Mapping[str, str]) -> None:
    if data.get("website", "").strip():
        raise LeadValidationError("registration could not be accepted")
    if data.get("consent", "").casefold() != "yes":
        raise LeadValidationError("privacy consent is required")
