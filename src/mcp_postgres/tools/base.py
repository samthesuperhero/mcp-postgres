"""Helpers shared by all tool modules."""

from __future__ import annotations

from ..capabilities import CapabilityError, CapabilityManager, DbTier, OsTier


def guard_or_error(
    caps: CapabilityManager,
    os_min: OsTier | None = None,
    db_min: DbTier | None = None,
    database: str | None = None,
):
    """Run the capability guard.

    Returns ``(True, notices)`` when the action is permitted (``notices`` is a
    possibly-empty list of capability-change strings), or ``(False, error_dict)``
    when it is refused — the error dict still carries any change notices so the
    caller is informed its privileges shifted, and the current ``database`` so it
    always knows which target the refusal applies to.
    """
    try:
        notices = caps.guard(os_min=os_min, db_min=db_min)
        return True, notices
    except CapabilityError as exc:
        err = {"ok": False, "error": str(exc), "capability_changed": exc.notices}
        if database is not None:
            err["database"] = database
        return False, err


def attach(result: dict, notices: list[str], database: str | None = None) -> dict:
    """Stamp a result with capability-change notices and the current ``database``.

    Every tool result carries ``database`` because the target is a session-wide
    "current" DB (set by ``use_database``); the caller must never be in doubt
    which database it just acted on.
    """
    if notices or database is not None:
        result = dict(result)
        if notices:
            result["capability_changed"] = notices
        if database is not None:
            result["database"] = database
    return result


def cell(value):
    """Coerce a DB cell into a JSON-friendly value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def rows_as_dicts(cols, rows):
    return [{c: cell(v) for c, v in zip(cols, r)} for r in rows]
