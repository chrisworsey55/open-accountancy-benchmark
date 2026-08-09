"""Versioned UK and US jurisdiction packs for deterministic world behaviour.

The public domain models intentionally remain jurisdiction-neutral.  This module
loads the pack-authored data under the repository-level ``jurisdictions/`` directory
and applies country-specific rules such as UK VAT-code validation and business-day
calendars at the world/tool boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping
from zoneinfo import ZoneInfo

from mirrorfirm.core.models import TaxTag

Jurisdiction = Literal["uk", "us"]
AccountType = Literal["asset", "liability", "equity", "income", "expense"]

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "jurisdictions"


@dataclass(frozen=True)
class AccountTemplate:
    """One chart-of-accounts row supplied by a jurisdiction pack."""

    code: str
    name: str
    type: AccountType
    tax_dimension: str | None
    active: bool


@dataclass(frozen=True)
class VatCode:
    """A UK VAT treatment and its deterministic tax rate."""

    code: str
    rate_bp: int
    description: str
    input_vat_recoverable: bool


@dataclass(frozen=True)
class BusinessCalendar:
    """Weekend and pack-shipped holiday rules for a simulated jurisdiction."""

    holidays: frozenset[date]
    weekend_days: frozenset[int] = frozenset({5, 6})

    def is_business_day(self, value: date) -> bool:
        """Return whether ``value`` is neither a weekend nor a pack holiday."""

        return value.weekday() not in self.weekend_days and value not in self.holidays

    def add_business_days(self, value: date, business_days: int) -> date:
        """Move a local calendar date by a signed number of business days."""

        if business_days == 0:
            return value

        direction = 1 if business_days > 0 else -1
        remaining = abs(business_days)
        current = value
        while remaining:
            current = date.fromordinal(current.toordinal() + direction)
            if self.is_business_day(current):
                remaining -= 1
        return current


@dataclass(frozen=True)
class JurisdictionPack:
    """The immutable data and calendar behaviour for one supported jurisdiction."""

    jurisdiction: Jurisdiction
    timezone: str
    terminology: Mapping[str, str]
    legal_forms: tuple[str, ...]
    chart_of_accounts: tuple[AccountTemplate, ...]
    business_calendar: BusinessCalendar
    vat_codes: Mapping[str, VatCode]
    vat_control_accounts: Mapping[str, str]

    @property
    def zoneinfo(self) -> ZoneInfo:
        """Return the IANA zone used for local tool-output rendering."""

        return ZoneInfo(self.timezone)

    def render_local(self, world_time: datetime) -> str:
        """Render an aware instant locally with an offset to disambiguate DST folds."""

        if world_time.tzinfo is None or world_time.utcoffset() is None:
            raise ValueError("world_time must be timezone-aware")
        local_time = world_time.astimezone(self.zoneinfo)
        return f"{local_time.isoformat(timespec='seconds')} [{self.timezone}]"

    def add_business_days(self, world_time: datetime, business_days: int) -> datetime:
        """Advance a UTC instant by local business days, preserving local wall time.

        Stored world times stay UTC.  Conversion through the pack's IANA zone means a
        spring-forward or fall-back changes the UTC offset without shifting the local
        time that an accounting-practice user sees.
        """

        if world_time.tzinfo is None or world_time.utcoffset() is None:
            raise ValueError("world_time must be timezone-aware")
        local_time = world_time.astimezone(self.zoneinfo)
        target_date = self.business_calendar.add_business_days(
            local_time.date(), business_days
        )
        target_local = datetime.combine(
            target_date,
            time(
                local_time.hour,
                local_time.minute,
                local_time.second,
                local_time.microsecond,
                tzinfo=self.zoneinfo,
                fold=local_time.fold,
            ),
        )
        return target_local.astimezone(timezone.utc)

    def vat_code(self, code: str) -> VatCode:
        """Return a named UK VAT code or reject it as unavailable in this pack."""

        try:
            return self.vat_codes[code]
        except KeyError as error:
            raise ValueError(
                f"VAT code {code!r} is not available in the {self.jurisdiction} pack"
            ) from error

    def validate_tax_tag(self, tax_tag: TaxTag | None) -> None:
        """Validate a typed tax tag against this pack's supported tax treatments."""

        if self.jurisdiction == "us":
            if tax_tag is not None:
                raise ValueError(
                    "US pack does not support TaxTag classifications in v0.1"
                )
            return
        if tax_tag is None:
            return

        vat_code = self.vat_code(tax_tag.code)
        if tax_tag.rate_bp != vat_code.rate_bp:
            raise ValueError(
                f"VAT code {tax_tag.code!r} requires rate_bp={vat_code.rate_bp}"
            )


def _pack_file(jurisdiction: Jurisdiction) -> Path:
    return _PACKS_ROOT / jurisdiction / "pack.json"


def _load_pack(jurisdiction: Jurisdiction) -> JurisdictionPack:
    raw = json.loads(_pack_file(jurisdiction).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{jurisdiction} pack must contain a JSON object")
    if raw.get("jurisdiction") != jurisdiction:
        raise ValueError(f"{jurisdiction} pack has an invalid jurisdiction identifier")

    terminology = raw["terminology"]
    legal_forms = raw["legal_forms"]
    account_rows = raw["chart_of_accounts"]
    holiday_dates = raw["holiday_dates"]
    vat_rows = raw["vat_codes"]
    vat_control_accounts = raw["vat_control_accounts"]

    if not isinstance(terminology, dict):
        raise ValueError(f"{jurisdiction} pack terminology must be an object")
    if not isinstance(legal_forms, list):
        raise ValueError(f"{jurisdiction} pack legal_forms must be a list")
    if not isinstance(account_rows, list):
        raise ValueError(f"{jurisdiction} pack chart_of_accounts must be a list")
    if not isinstance(holiday_dates, list):
        raise ValueError(f"{jurisdiction} pack holiday_dates must be a list")
    if not isinstance(vat_rows, list):
        raise ValueError(f"{jurisdiction} pack vat_codes must be a list")
    if not isinstance(vat_control_accounts, dict):
        raise ValueError(f"{jurisdiction} pack vat_control_accounts must be an object")

    chart_of_accounts = tuple(
        AccountTemplate(
            code=str(row["code"]),
            name=str(row["name"]),
            type=row["type"],
            tax_dimension=(
                None if row["tax_dimension"] is None else str(row["tax_dimension"])
            ),
            active=bool(row["active"]),
        )
        for row in account_rows
    )
    vat_codes = {
        str(row["code"]): VatCode(
            code=str(row["code"]),
            rate_bp=int(row["rate_bp"]),
            description=str(row["description"]),
            input_vat_recoverable=bool(row["input_vat_recoverable"]),
        )
        for row in vat_rows
    }
    return JurisdictionPack(
        jurisdiction=jurisdiction,
        timezone=str(raw["timezone"]),
        terminology=MappingProxyType(
            {str(key): str(value) for key, value in terminology.items()}
        ),
        legal_forms=tuple(str(value) for value in legal_forms),
        chart_of_accounts=chart_of_accounts,
        business_calendar=BusinessCalendar(
            frozenset(date.fromisoformat(str(value)) for value in holiday_dates)
        ),
        vat_codes=MappingProxyType(vat_codes),
        vat_control_accounts=MappingProxyType(
            {str(key): str(value) for key, value in vat_control_accounts.items()}
        ),
    )


UK_PACK = _load_pack("uk")
US_PACK = _load_pack("us")
_PACKS: Mapping[Jurisdiction, JurisdictionPack] = MappingProxyType(
    {"uk": UK_PACK, "us": US_PACK}
)


def get_pack(jurisdiction: Jurisdiction) -> JurisdictionPack:
    """Return the installed pack for ``uk`` or ``us``."""

    return _PACKS[jurisdiction]


__all__ = [
    "AccountTemplate",
    "BusinessCalendar",
    "Jurisdiction",
    "JurisdictionPack",
    "UK_PACK",
    "US_PACK",
    "VatCode",
    "get_pack",
]
