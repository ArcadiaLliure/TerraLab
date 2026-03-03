import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

from TerraLab.common.utils import get_base_dir


METNO_COMPACT_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
METNO_USER_AGENT = "TerraLab/1.0 (weather client)"
METNO_FORECAST_MAX_DAYS = 10
METNO_CACHE_TTL_SECONDS = 12 * 3600


def _clamp(value, low, high):
    return max(low, min(high, float(value)))


def _to_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _extract_precip_rate_mm_h(data):
    # Prefer 1-hour buckets. Fallback to 6h/12h transformed to mm/h.
    for key, hours in (("next_1_hours", 1.0), ("next_6_hours", 6.0), ("next_12_hours", 12.0)):
        block = data.get(key, {}) if isinstance(data, dict) else {}
        details = block.get("details", {}) if isinstance(block, dict) else {}
        amount = _to_float(details.get("precipitation_amount"), None)
        if amount is not None:
            summary = block.get("summary", {}) if isinstance(block, dict) else {}
            symbol = str(summary.get("symbol_code", "") or "").lower()
            return max(0.0, amount / max(1.0, hours)), symbol
    return 0.0, ""


def _compact_metno_payload(payload):
    props = payload.get("properties", {}) if isinstance(payload, dict) else {}
    timeseries = props.get("timeseries", []) if isinstance(props, dict) else []

    records = {}
    start_dt = None
    end_dt = None

    for item in timeseries:
        dt = _parse_iso_utc(item.get("time") if isinstance(item, dict) else None)
        if dt is None:
            continue
        if start_dt is None or dt < start_dt:
            start_dt = dt
        if end_dt is None or dt > end_dt:
            end_dt = dt

        data = item.get("data", {}) if isinstance(item, dict) else {}
        instant = data.get("instant", {}) if isinstance(data, dict) else {}
        details = instant.get("details", {}) if isinstance(instant, dict) else {}

        cloud_area_fraction = _to_float(details.get("cloud_area_fraction"), 0.0)
        cloud_cover = _clamp(cloud_area_fraction / 100.0, 0.0, 1.0)

        air_temperature = _to_float(details.get("air_temperature"), None)
        precip_rate_mm_h, symbol = _extract_precip_rate_mm_h(data)

        precipitation_type = "none"
        if precip_rate_mm_h > 0.03:
            if air_temperature is not None and air_temperature <= 0.5:
                precipitation_type = "snow"
            else:
                precipitation_type = "rain"

        precipitation_intensity = _clamp(precip_rate_mm_h / 6.0, 0.0, 1.0)
        thunder_probability = 0.35 if "thunder" in symbol else 0.0

        if precipitation_type != "none":
            # Keep visual consistency: precipitation implies relevant cloud cover.
            cloud_cover = max(cloud_cover, 0.65 + 0.25 * precipitation_intensity)

        day_index = (dt.date() - datetime(dt.year, 1, 1, tzinfo=timezone.utc).date()).days
        key = f"{dt.year}:{day_index}:{dt.hour}"
        records[key] = {
            "cloud_cover": cloud_cover,
            "precipitation_type": precipitation_type,
            "precipitation_intensity": precipitation_intensity,
            "thunder_probability": thunder_probability,
        }

    fetched_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "fetched_utc": fetched_utc,
        "coverage_start": start_dt.isoformat().replace("+00:00", "Z") if start_dt else None,
        "coverage_end": end_dt.isoformat().replace("+00:00", "Z") if end_dt else None,
        "records": records,
    }


def _fetch_metno_compact(lat, lon, timeout_s, user_agent):
    try:
        import requests

        url = f"{METNO_COMPACT_URL}?lat={float(lat):.6f}&lon={float(lon):.6f}"
        headers = {
            "User-Agent": str(user_agent or METNO_USER_AGENT),
            "Accept": "application/json",
        }
        response = requests.get(url, headers=headers, timeout=float(timeout_s))
        if response.status_code != 200:
            return None
        payload = response.json()
    except Exception:
        return None
    try:
        return _compact_metno_payload(payload)
    except Exception:
        return None


class MetNoWeatherProvider:
    def __init__(self, latitude=0.0, longitude=0.0):
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self._executor = ProcessPoolExecutor(max_workers=1)
        self._future = None
        self._last_attempt_monotonic = 0.0
        self._attempt_backoff_seconds = 20.0
        self._cache = {}
        self._cache_path = os.path.join(get_base_dir(), "terralab_metno_weather_cache.json")
        self._load_cache()

    def shutdown(self):
        try:
            if self._future is not None:
                self._future.cancel()
        except Exception:
            pass
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def __del__(self):
        self.shutdown()

    def _load_cache(self):
        try:
            if os.path.exists(self._cache_path):
                with open(self._cache_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._cache = data
        except Exception:
            self._cache = {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def set_location(self, latitude, longitude):
        latitude = float(latitude)
        longitude = float(longitude)
        if abs(self.latitude - latitude) < 1e-6 and abs(self.longitude - longitude) < 1e-6:
            return
        self.latitude = latitude
        self.longitude = longitude
        self._cache = {}
        self._save_cache()
        self._future = None
        self._last_attempt_monotonic = 0.0

    def _cache_fresh(self):
        fetched = _parse_iso_utc(self._cache.get("fetched_utc") if isinstance(self._cache, dict) else None)
        if fetched is None:
            return False
        age = abs((datetime.now(timezone.utc) - fetched).total_seconds())
        return age < METNO_CACHE_TTL_SECONDS

    def _cache_matches_location(self):
        if not isinstance(self._cache, dict):
            return False
        lat = _to_float(self._cache.get("lat"), None)
        lon = _to_float(self._cache.get("lon"), None)
        if lat is None or lon is None:
            return False
        return abs(lat - self.latitude) < 1e-6 and abs(lon - self.longitude) < 1e-6

    def _coverage_contains(self, target_date):
        if not isinstance(self._cache, dict):
            return False
        start = _parse_iso_utc(self._cache.get("coverage_start"))
        end = _parse_iso_utc(self._cache.get("coverage_end"))
        if start is None or end is None:
            return False
        return start.date() <= target_date <= end.date()

    def _within_remote_range(self, target_date):
        today = datetime.now(timezone.utc).date()
        return today <= target_date <= (today + timedelta(days=METNO_FORECAST_MAX_DAYS))

    def _start_background_fetch(self):
        if self._future is not None and not self._future.done():
            return
        now_m = time.monotonic()
        if now_m - self._last_attempt_monotonic < self._attempt_backoff_seconds:
            return
        self._last_attempt_monotonic = now_m
        self._future = self._executor.submit(
            _fetch_metno_compact,
            self.latitude,
            self.longitude,
            8.0,
            METNO_USER_AGENT,
        )

    def _consume_ready_future(self):
        if self._future is None or not self._future.done():
            return
        try:
            result = self._future.result(timeout=0.0)
        except Exception:
            result = None
        self._future = None
        if isinstance(result, dict) and isinstance(result.get("records"), dict) and result.get("records"):
            result["lat"] = self.latitude
            result["lon"] = self.longitude
            self._cache = result
            self._save_cache()

    @staticmethod
    def _target_date_from_year_day(year, day_of_year):
        year_i = int(year)
        day_i = int(day_of_year)
        return (datetime(year_i, 1, 1) + timedelta(days=day_i)).date()

    def _should_fetch(self, target_date):
        if not self._within_remote_range(target_date):
            return False
        if not self._cache_matches_location():
            return True
        if not self._coverage_contains(target_date):
            return True
        return not self._cache_fresh()

    def get_weather(self, year, day_of_year, hour):
        target_date = self._target_date_from_year_day(year, day_of_year)
        self._consume_ready_future()

        if self._should_fetch(target_date):
            self._start_background_fetch()

        if not self._cache_matches_location():
            return None
        if not self._coverage_contains(target_date):
            return None

        records = self._cache.get("records", {}) if isinstance(self._cache, dict) else {}
        key = f"{int(year)}:{int(day_of_year)}:{int(hour) % 24}"
        item = records.get(key)
        if isinstance(item, dict):
            return item
        return None
