"""Helpers shared by all tool modules."""

from __future__ import annotations

from ..capabilities import CapabilityError, CapabilityManager, DbTier, OsTier


def guard_or_error(caps: CapabilityManager, os_min: OsTier | None = None, db_min: DbTier | None = None):
    """Run the capability guard.

    Returns ``(True, notices)`` when the action is permitted (``notices`` is a
    possibly-empty list of capability-change strings), or ``(False, error_dict)``
    when it is refused — the error dict still carries any change notices so the
    caller is informed its privileges shifted.
    """
    try:
        notices = caps.guard(os_min=os_min, db_min=db_min)
        return True, notices
    except CapabilityError as exc:
        return False, {"ok": False, "error": str(exc), "capability_changed": exc.notices}


def attach(result: dict, notices: list[str]) -> dict:
    """Attach capability-change notices to a successful result, if any."""
    if notices:
        result = dict(result)
        result["capability_changed"] = notices
    return result


def cell(value):
    """Coerce a DB cell into a JSON-friendly value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def rows_as_dicts(cols, rows):
    return [{c: cell(v) for c, v in zip(cols, r)} for r in rows]
