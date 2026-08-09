"""WP-04 jurisdiction-pack calendar, VAT, terminology, and CoA coverage."""

from datetime import date, datetime, timezone

import pytest

from mirrorfirm.core.models import TaxTag
from mirrorfirm.jurisdictions import UK_PACK, US_PACK, JurisdictionPack, get_pack


def test_pack_data_includes_terminology_legal_forms_and_coa_templates() -> None:
    assert get_pack("uk") is UK_PACK
    assert get_pack("us") is US_PACK
    assert UK_PACK.terminology["tax"] == "VAT"
    assert US_PACK.terminology["vendor_tax_form"] == "W-9"
    assert "limited_company" in UK_PACK.legal_forms
    assert "limited_liability_company" in US_PACK.legal_forms
    assert {account.code for account in UK_PACK.chart_of_accounts} >= {
        "2201",
        "2202",
        "7502",
    }
    assert {account.code for account in US_PACK.chart_of_accounts} >= {"1000", "2100"}


def test_pack_business_calendars_apply_weekends_and_fixed_holidays() -> None:
    assert not UK_PACK.business_calendar.is_business_day(date(2026, 5, 25))
    assert UK_PACK.business_calendar.add_business_days(date(2026, 5, 22), 1) == date(
        2026, 5, 26
    )
    assert not US_PACK.business_calendar.is_business_day(date(2026, 7, 3))
    assert US_PACK.business_calendar.add_business_days(date(2026, 7, 2), 1) == date(
        2026, 7, 6
    )


@pytest.mark.parametrize(
    ("pack", "instant", "expected"),
    [
        (
            UK_PACK,
            datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc),
            "2026-03-29T02:30:00+01:00 [Europe/London]",
        ),
        (
            US_PACK,
            datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc),
            "2026-03-08T03:30:00-04:00 [America/New_York]",
        ),
        (
            UK_PACK,
            datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc),
            "2026-10-25T01:30:00+00:00 [Europe/London]",
        ),
        (
            US_PACK,
            datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),
            "2026-11-01T01:30:00-05:00 [America/New_York]",
        ),
    ],
)
def test_local_rendering_is_unambiguous_across_dst_boundaries(
    pack: JurisdictionPack, instant: datetime, expected: str
) -> None:
    assert pack.render_local(instant) == expected


def test_business_day_advancement_preserves_local_time_across_dst() -> None:
    friday_before_uk_dst = datetime(2026, 3, 27, 9, 30, tzinfo=timezone.utc)

    assert UK_PACK.add_business_days(friday_before_uk_dst, 1) == datetime(
        2026, 3, 30, 8, 30, tzinfo=timezone.utc
    )


def test_uk_vat_table_includes_blocked_and_enforces_pack_rates() -> None:
    assert UK_PACK.vat_control_accounts == {
        "input": "acc-2202",
        "output": "acc-2201",
    }
    assert UK_PACK.vat_code("blocked").input_vat_recoverable is False

    for code, rate_bp in {
        "20-std": 2000,
        "5-red": 500,
        "0-zero": 0,
        "exempt": 0,
        "blocked": 2000,
    }.items():
        UK_PACK.validate_tax_tag(TaxTag(kind="uk_vat", code=code, rate_bp=rate_bp))

    with pytest.raises(ValueError, match="requires rate_bp=2000"):
        UK_PACK.validate_tax_tag(TaxTag(kind="uk_vat", code="blocked", rate_bp=0))
    with pytest.raises(ValueError, match="not available"):
        UK_PACK.validate_tax_tag(TaxTag(kind="uk_vat", code="unknown", rate_bp=0))
    with pytest.raises(ValueError, match="does not support TaxTag"):
        US_PACK.validate_tax_tag(TaxTag(kind="uk_vat", code="20-std", rate_bp=2000))
