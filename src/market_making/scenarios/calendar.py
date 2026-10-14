"""XBUE trading sessions; settlement-only holidays remain trading days."""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

BA = timezone(timedelta(hours=-3))
# Checked 2026-10-14 against https://www.byma.com.ar/mercado/calendario-bursatil.
# The exchange library's general rules do not cover every extraordinary closure
# or BYMA's distinction between trading and local settlement holidays.
BYMA_2026_CLOSED = frozenset(date.fromisoformat("2026-" + day) for day in (
    "01-01", "02-16", "02-17", "03-24", "04-02", "04-03", "05-01",
    "05-25", "06-15", "07-09", "08-17", "10-12", "11-09", "11-10",
    "11-23", "12-08", "12-25", "12-31",
))
BYMA_2026_OPEN = frozenset(date.fromisoformat("2026-" + day) for day in (
    "03-23", "07-10", "11-06", "12-07", "12-24",
))


def today():
    return datetime.now(BA).date()


@lru_cache(maxsize=16)
def year_sessions(year):
    if not 2000 <= year <= 2099:
        raise ValueError("Trading calendar supports years 2000 through 2099.")
    # Import lazily so unrelated command-line tools do not load pandas/numpy.
    from exchange_calendars.exchange_calendar_xbue import XBUEExchangeCalendar
    calendar = XBUEExchangeCalendar(start=f"{year}-01-01", end=f"{year}-12-31")
    sessions = {stamp.date() for stamp in calendar.sessions}
    if year == 2026:
        sessions.difference_update(BYMA_2026_CLOSED)
        sessions.update(BYMA_2026_OPEN)
    return frozenset(sessions)


def is_session(day):
    return day in year_sessions(day.year)


def validate_horizon(count):
    if type(count) is not int or not 1 <= count <= 252:
        raise ValueError("Enter a whole number of trading days between 1 and 252.")
    return count


def advance_sessions(day, count=2):
    """Count complete trading dates strictly AFTER the reference date."""
    validate_horizon(count)
    if type(day) is not date:
        raise ValueError("Use a reference date and a positive number of sessions.")
    result = day
    while count:
        result += timedelta(days=1)
        if is_session(result):
            count -= 1
    return result


def sessions_between(start, end):
    if start > end:
        return ()
    return tuple(sorted(day for year in range(start.year, end.year + 1)
                        for day in year_sessions(year) if start <= day <= end))
