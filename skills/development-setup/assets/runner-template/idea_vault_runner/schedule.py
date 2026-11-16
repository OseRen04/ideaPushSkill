"""Validate user schedules and calculate bounded UTC execution windows."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import re
from typing import List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .errors import BlockedError


_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_UTC = timezone.utc


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous settings instead of silently selecting the last key."""


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "Unhashable mapping key.", key_node.start_mark)
        if duplicate:
            raise yaml.constructor.ConstructorError(None, None, "Duplicate mapping key.", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _invalid(message):
    raise BlockedError(message, "invalid_schedule")


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        _invalid("Schedule times must use HH:MM format.")
    try:
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)
    except ValueError:
        _invalid("Schedule times must be valid 24-hour times.")


@dataclass(frozen=True)
class Schedule:
    enabled: bool
    timezone: str
    days: Tuple[str, ...]
    start_time: str
    end_time: str
    max_duration_minutes: int
    max_concurrent_jobs: int

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping):
            _invalid("The schedule must be a mapping.")
        if type(value.get("enabled")) is not bool:
            _invalid("Schedule enabled must be a boolean.")
        zone = value.get("timezone")
        if not isinstance(zone, str) or not zone:
            _invalid("An IANA timezone is required.")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            _invalid("The IANA timezone is unavailable.")
        schedule = value.get("schedule")
        limits = value.get("limits")
        if not isinstance(schedule, Mapping) or not isinstance(limits, Mapping):
            _invalid("Schedule and limits mappings are required.")
        days = schedule.get("days")
        if not isinstance(days, list) or not days or any(day not in _DAYS for day in days):
            _invalid("Schedule days must be a nonempty list of weekday names.")
        if len(set(days)) != len(days):
            _invalid("Schedule days must not repeat.")
        start, end = schedule.get("start_time"), schedule.get("end_time")
        if _clock(start) == _clock(end):
            _invalid("Start and end times must differ.")
        duration = limits.get("max_duration_minutes")
        concurrency = limits.get("max_concurrent_jobs")
        if type(duration) is not int or duration <= 0:
            _invalid("Maximum duration must be a positive integer.")
        if type(concurrency) is not int or concurrency != 1:
            _invalid("Exactly one concurrent development job is supported.")
        return cls(value["enabled"], zone, tuple(days), start, end, duration, concurrency)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    deadline: datetime
    work_deadline: datetime


def load_schedule(path: Path) -> Schedule:
    try:
        data = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError):
        _invalid("The schedule file cannot be read as YAML.")
    return Schedule.from_dict(data)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid("Execution times must be timezone-aware datetimes.")
    return value.astimezone(_UTC)


def _local_boundary(day: date, clock: time, zone: ZoneInfo) -> Optional[datetime]:
    naive = datetime.combine(day, clock)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    valid_first = first.astimezone(_UTC).astimezone(zone).replace(tzinfo=None) == naive
    valid_second = second.astimezone(_UTC).astimezone(zone).replace(tzinfo=None) == naive
    if not valid_first or not valid_second:
        return None
    if first.utcoffset() != second.utcoffset():
        # An ambiguous wall-clock boundary requires a user decision, not a guess.
        return None
    return first.astimezone(_UTC)


def execution_window(schedule: Schedule, now: datetime, started_at: datetime = None,
                     require_work_time: bool = True) -> Optional[Window]:
    now_utc = _aware(now)
    started_utc = now_utc if started_at is None else _aware(started_at)
    if started_utc > now_utc:
        _invalid("The actual job start cannot be in the future.")
    if not schedule.enabled:
        return None
    zone = ZoneInfo(schedule.timezone)
    local_day = now_utc.astimezone(zone).date()
    start_clock, end_clock = _clock(schedule.start_time), _clock(schedule.end_time)
    overnight = end_clock < start_clock
    for day in (local_day, local_day - timedelta(days=1)):
        if _DAYS[day.weekday()] not in schedule.days:
            continue
        end_day = day + timedelta(days=1) if overnight else day
        start = _local_boundary(day, start_clock, zone)
        end = _local_boundary(end_day, end_clock, zone)
        if start is None or end is None or not start <= now_utc < end:
            continue
        try:
            deadline = min(end, started_utc + timedelta(minutes=schedule.max_duration_minutes))
        except OverflowError:
            _invalid("Maximum duration is outside the supported date range.")
        work_deadline = deadline - timedelta(minutes=5)
        if now_utc >= deadline or (require_work_time and now_utc >= work_deadline):
            return None
        return Window(start, end, deadline, work_deadline)
    return None


def cron_entries(schedule: Schedule) -> List[dict]:
    if not schedule.enabled:
        return []
    clock = _clock(schedule.start_time)
    days = ",".join(day for day in _DAYS if day in schedule.days)
    return [{"cron": "{} {} * * {}".format(clock.minute, clock.hour, days), "timezone": schedule.timezone}]
