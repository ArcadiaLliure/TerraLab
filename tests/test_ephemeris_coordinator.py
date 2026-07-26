from datetime import datetime, timezone

from TerraLab.astro.ephemeris_coordinator import (
    EphemerisCoordinator,
    snapshot_matches_utc_context,
    utc_datetime_from_context,
)


def test_ephemeris_context_uses_zero_based_day_of_year():
    # 12 August 2026 is index 223 when 1 January is index zero.
    timestamp = utc_datetime_from_context(2026, 223, 18.5)

    assert timestamp == datetime(2026, 8, 12, 18, 30, tzinfo=timezone.utc)


def test_coordinator_snapshot_timestamp_does_not_shift_eclipse_date():
    coordinator = EphemerisCoordinator()
    coordinator._ts = None
    coordinator._eph = None
    try:
        snapshot = coordinator._compute_snapshot(
            year_utc=2026,
            day_of_year_utc=223,
            ut_hour=18.5,
            latitude=41.60775,
            longitude=0.60794,
        )
    finally:
        coordinator.shutdown()

    assert snapshot["timestamp_utc"] == "2026-08-12T18:30:00+00:00"


def test_subminute_eclipse_rejects_snapshot_two_minutes_out_of_date():
    stale = {"timestamp_utc": "2026-08-12T18:27:18+00:00"}
    fresh = {"timestamp_utc": "2026-08-12T18:29:17.250000+00:00"}
    context = {
        "year_utc": 2026,
        "day_of_year_utc": 223,
        "ut_hour": 18 + 29 / 60 + 18 / 3600,
    }

    assert not snapshot_matches_utc_context(stale, **context)
    assert snapshot_matches_utc_context(fresh, **context)
