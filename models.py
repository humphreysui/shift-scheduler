"""Shift Scheduler — shared constants and helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

APP_NAME = "Shift Scheduler"
CYCLE_DAYS = 28
MAX_RESOURCES = 9
MIN_RESOURCES = 7  # 6 resources x 5 days = 30 < 31 person-days needed per week

SHIFTS = ["S1", "S2", "S3", "S4"]
OFF = "Off"
VALUES = [OFF] + SHIFTS

SHIFT_INFO = {
    "S1": {"name": "Shift 1 · Office day", "short": "S1", "hours": "08:00–17:00"},
    "S2": {"name": "Shift 2 · Evening", "short": "S2", "hours": "14:00–23:00"},
    "S3": {"name": "Shift 3 · Night", "short": "S3", "hours": "23:00–08:00 (+1)"},
    "S4": {"name": "Shift 4 · Support", "short": "S4", "hours": "07:00–16:00"},
}

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Per-day coverage minimums / caps by day type.
COVERAGE = {
    "workday": {"morning_min": 3, "morning_max": 4, "s2_min": 1, "s2_max": 2, "s3_min": 1, "s3_max": 2},
    "light":   {"morning_min": 1, "morning_max": 3, "s2_min": 1, "s2_max": 2, "s3_min": 1, "s3_max": 2},
}

WEIGHT_FLOOR = 7   # weekday morning weight: below this -> best effort + flag
WEIGHT_TARGET = 9  # optimization target for weekday mornings
WORK_STREAK_MAX = 5
NIGHT_RUN_MAX = 3
COMP_AFTER = 2     # comp day after a run of >= 2 nights (when coverage allows)


@dataclass
class Resource:
    id: str
    name: str
    weight: int = 2          # 1..3 (3 = highest availability weight)
    off_pair_start: int = 0  # 0=Mon .. 6=Sun -> off days are start and start+1
    active: bool = True

    @property
    def off_days(self) -> tuple[int, int]:
        return (self.off_pair_start, (self.off_pair_start + 1) % 7)


def off_pair_label(start: int) -> str:
    start %= 7
    return f"{WEEKDAY_NAMES[start]}+{WEEKDAY_NAMES[(start + 1) % 7]}"


def day_type(d: date, light_days: set[str] | None = None) -> str:
    """'light' days (weekends, holidays) run reduced coverage."""
    if d.weekday() >= 5:
        return "light"
    if light_days and d.isoformat() in light_days:
        return "light"
    return "workday"


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
