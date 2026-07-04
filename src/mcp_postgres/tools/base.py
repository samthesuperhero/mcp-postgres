"""Helpers shared by all tool modules."""

from __future__ import annotations

from ..capabilities import CapabilityError, CapabilityManager, DbTier, OsTier


def guard_or_error(
    caps: CapabilityManager,
    os_min: OsTier | None = None,
    db_min: DbTier | None = None,
    db_needs: tuple[str, ...] | None = None,
    database: str | None = None,
):
    """Run the capability guard.

    Returns ``(True, notices)`` when the action is permitted (``notices`` is a
    possibly-empty list of capability-change strings), or ``(False, error_dict)``
    when it is refused — the error dict still carries any change notices so the
    caller is informed its privileges shifted, and the current ``database`` so it
    always knows which target the refusal applies to.

    ``db_needs`` names attribute-driven DB capabilities (``createdb``/``createrole``)
    the action requires, independent of the admin tier.
    """
    try:
        notices = caps.guard(os_min=os_min, db_min=db_min, db_needs=db_needs)
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
    """Coerce a DB cell into a JSON-friendly value.

    Scalars pass through; lists/tuples and dicts are coerced *recursively* so array
    columns (e.g. an index's column list, an enum's labels) and composite/JSON values
    survive as real JSON arrays/objects instead of being stringified. Anything else
    (dates, Decimals, UUIDs, …) falls back to ``str()``.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [cell(v) for v in value]
    if isinstance(value, dict):
        return {k: cell(v) for k, v in value.items()}
    return str(value)


def rows_as_dicts(cols, rows):
    return [{c: cell(v) for c, v in zip(cols, r)} for r in rows]
