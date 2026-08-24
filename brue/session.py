# in_session() timezone support. The engine deliberately avoids tzdata and
# zoneinfo (standard library only, so results never depend on the host's
# timezone database) and implements the standard DST rules algorithmically
# for the zones Brue's named sessions use plus the common custom-session tz
# values. Rules match the IANA database for all dates >= 2007 (post-2007 US
# rules; EU rules stable since 1996; AU stable since 2008). An unknown
# timezone returns None, which in_session() treats as False.
import math
from dataclasses import dataclass
from typing import Optional, Tuple

# Signed 64-bit range. Float-to-integer conversion saturates at these
# bounds; Python ints are unbounded, so the clamp keeps out-of-range (but
# finite) timestamps on the same saturating rule.
_I64_MIN = -(2 ** 63)
_I64_MAX = 2 ** 63 - 1


def _f64_to_i64(x: float) -> int:
    # Truncate toward zero, then saturate to the 64-bit range.
    ms = int(x)  # int() truncates toward zero
    if ms < _I64_MIN:
        return _I64_MIN
    if ms > _I64_MAX:
        return _I64_MAX
    return ms


def _div_trunc(a: int, b: int) -> int:
    # Integer division truncating toward zero; Python // floors. The civil
    # date algorithms below adjust their numerators expecting truncating
    # division, so floor division would be off by one for negative years.
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def days_from_civil(y: int, m: int, d: int) -> int:
    """Days from civil date (Howard Hinnant's algorithm)."""
    if m <= 2:
        y = y - 1
    era = _div_trunc(y if y >= 0 else y - 399, 400)
    yoe = y - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(z: int) -> Tuple[int, int, int]:
    """Civil date from days since epoch."""
    z = z + 719468
    era = _div_trunc(z if z >= 0 else z - 146096, 146097)
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y, m, d)


def weekday_from_days(z: int) -> int:
    """0=Sunday..6=Saturday for a days-since-epoch value."""
    # Python % is a non-negative remainder for a positive modulus.
    return (z + 4) % 7


def nth_weekday_utc_ms(year: int, month: int, weekday: int, nth: int, hour_utc: int) -> int:
    """UTC ms of the Nth (1-based) given weekday of a month, at a UTC hour."""
    first = days_from_civil(year, month, 1)
    first_wd = weekday_from_days(first)
    offset = (weekday + 7 - first_wd) % 7
    day = first + offset + 7 * (nth - 1)
    return day * 86_400_000 + hour_utc * 3_600_000


def last_weekday_utc_ms(year: int, month: int, weekday: int, hour_utc: int) -> int:
    """UTC ms of the LAST given weekday of a month, at a UTC hour."""
    if month == 12:
        next_month_first = days_from_civil(year + 1, 1, 1)
    else:
        next_month_first = days_from_civil(year, month + 1, 1)
    day = next_month_first - 1
    while weekday_from_days(day) != weekday:
        day -= 1
    return day * 86_400_000 + hour_utc * 3_600_000


def year_of_ms(ms: int) -> int:
    # Python // is the Euclidean quotient for a positive divisor.
    return civil_from_days(ms // 86_400_000)[0]


def utc_offset_minutes(tz: str, ms: int) -> Optional[int]:
    """UTC offset in minutes for the given zone at the given UTC instant."""
    year = year_of_ms(ms)
    if tz == "UTC":
        return 0
    # EU: DST last Sunday March 01:00 UTC to last Sunday October 01:00 UTC
    if tz == "Europe/London":
        start = last_weekday_utc_ms(year, 3, 0, 1)
        end = last_weekday_utc_ms(year, 10, 0, 1)
        return 60 if start <= ms < end else 0
    if tz in ("Europe/Berlin", "Europe/Paris", "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam"):
        start = last_weekday_utc_ms(year, 3, 0, 1)
        end = last_weekday_utc_ms(year, 10, 0, 1)
        return 120 if start <= ms < end else 60
    # US: DST 2nd Sunday March 02:00 local-standard to 1st Sunday Nov 02:00 local-DST
    if tz == "America/New_York":
        start = nth_weekday_utc_ms(year, 3, 0, 2, 7)  # 02:00 EST = 07:00 UTC
        end = nth_weekday_utc_ms(year, 11, 0, 1, 6)  # 02:00 EDT = 06:00 UTC
        return -240 if start <= ms < end else -300
    if tz == "America/Chicago":
        start = nth_weekday_utc_ms(year, 3, 0, 2, 8)
        end = nth_weekday_utc_ms(year, 11, 0, 1, 7)
        return -300 if start <= ms < end else -360
    if tz == "America/Los_Angeles":
        start = nth_weekday_utc_ms(year, 3, 0, 2, 10)
        end = nth_weekday_utc_ms(year, 11, 0, 1, 9)
        return -420 if start <= ms < end else -480
    if tz == "Asia/Tokyo":
        return 540
    if tz in ("Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai"):
        return 480
    # AU: DST 1st Sunday October 02:00 local-std to 1st Sunday April 03:00 local-DST
    # (southern hemisphere: DST wraps the new year)
    if tz == "Australia/Sydney":
        # 02:00 AEST = Saturday 16:00 UTC; 03:00 AEDT = Saturday 16:00 UTC
        dst_start = nth_weekday_utc_ms(year, 10, 0, 1, 0) - 8 * 3_600_000  # Sun 02:00 +10 is Sat 16:00 UTC
        dst_end = nth_weekday_utc_ms(year, 4, 0, 1, 0) - 8 * 3_600_000  # Sun 03:00 +11 is Sat 16:00 UTC
        return 660 if (ms >= dst_start or ms < dst_end) else 600
    return None


def local_hm_dow(tz: str, ts_ms: float) -> Optional[Tuple[int, int, int]]:
    """Local (hour, minute, weekday 0=Sun) of a UTC ms timestamp in a zone."""
    if not math.isfinite(ts_ms):
        return None
    # The ms value is truncated toward zero before splitting into fields.
    ms = _f64_to_i64(ts_ms)
    offset = utc_offset_minutes(tz, ms)
    if offset is None:
        return None
    local = ms + offset * 60_000
    days = local // 86_400_000
    ms_of_day = local % 86_400_000
    h = ms_of_day // 3_600_000
    m = (ms_of_day % 3_600_000) // 60_000
    return (h, m, weekday_from_days(days))


def in_session_range(
    ts_ms: float,
    sh: int,
    sm: int,
    eh: int,
    em: int,
    tz: str,
    weekdays_only: bool,
) -> bool:
    """Whether the timestamp's local time falls in [start, end); a window
    with start > end wraps past midnight."""
    hmd = local_hm_dow(tz, ts_ms)
    if hmd is None:
        return False
    h, m, dow = hmd
    if weekdays_only and (dow == 0 or dow == 6):
        return False
    now = h * 60 + m
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= now < end
    return now >= start or now < end


@dataclass
class NamedSession:
    tz: str
    sh: int
    sm: int
    eh: int
    em: int


def named_session(name: str) -> Optional[NamedSession]:
    if name == "london":
        return NamedSession(tz="Europe/London", sh=8, sm=0, eh=16, em=30)
    if name == "new_york":
        return NamedSession(tz="America/New_York", sh=9, sm=30, eh=16, em=0)
    if name == "tokyo":
        return NamedSession(tz="Asia/Tokyo", sh=9, sm=0, eh=15, em=30)
    if name == "sydney":
        return NamedSession(tz="Australia/Sydney", sh=10, sm=0, eh=16, em=0)
    if name == "frankfurt":
        return NamedSession(tz="Europe/Berlin", sh=9, sm=0, eh=17, em=30)
    return None


@dataclass
class UtcParts:
    """UTC date components for the time builtins (year/month/.../second).
    The ms value is truncated toward zero before splitting."""

    year: float
    month: float  # 1-12
    day: float
    weekday: float  # 0=Sun
    hour: float
    minute: float
    second: float


def utc_parts(ts_ms: float) -> Optional[UtcParts]:
    if ts_ms != ts_ms:  # NaN
        return None
    # Timestamps beyond +-8.64e15 ms are an invalid date: every part is NaN.
    if abs(ts_ms) > 8.64e15:
        return None
    ms = _f64_to_i64(ts_ms)
    days = ms // 86_400_000
    ms_of_day = ms % 86_400_000
    y, m, d = civil_from_days(days)
    return UtcParts(
        year=float(y),
        month=float(m),
        day=float(d),
        weekday=float(weekday_from_days(days)),
        hour=float(ms_of_day // 3_600_000),
        minute=float((ms_of_day % 3_600_000) // 60_000),
        second=float((ms_of_day % 60_000) // 1000),
    )
