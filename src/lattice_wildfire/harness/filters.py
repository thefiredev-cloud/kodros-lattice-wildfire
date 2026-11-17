"""Entity stream ``Statement`` filters evaluated over entity JSON (camelCase, as published).

Supported: ``and``/``or``/``not``/``predicate``/``list`` (ANY_OF) with string, numeric, boolean, timestamp,
list and range values. Rejected with :class:`FilterError` (HTTP 400): ``enumType`` (Lattice compares
proto enum numbers, which REST JSON does not carry), ``COMPARATOR_WITHIN`` and geometric value types.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .timeutil import parse_time

Pred = Callable[[Any], bool]


class FilterError(ValueError):
    pass


def _camel(segment: str) -> str:
    head, *rest = segment.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _lookup(obj: Any, path: str) -> Any:
    cur = obj
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg) if seg in cur else cur.get(_camel(seg))
        if cur is None:
            return None
    return cur


def _scalar(value: dict) -> Any:
    """Python value of a Lattice ``Value`` holding a scalar."""
    if "enumType" in value:
        raise FilterError("enumType filter values are not supported by the harness (proto enum numbers)")
    for unsupported in ("boundedShapeType", "positionType", "headingType"):
        if unsupported in value:
            raise FilterError(f"{unsupported} filter values are not supported by the harness")
    if "booleanType" in value:
        return bool(value["booleanType"].get("value", False))
    if "stringType" in value:
        return value["stringType"].get("value", "")
    if "timestampType" in value:
        return parse_time(value["timestampType"].get("value", ""))
    if "numericType" in value:
        num = value["numericType"]
        for key in ("doubleValue", "floatValue", "int32Value", "int64Value", "uint32Value", "uint64Value"):
            if key in num:
                return float(num[key])
        raise FilterError("numericType has no value")
    raise FilterError("filter value has no supported type")


def _coerce(field: Any, ref: Any) -> Any:
    if isinstance(ref, datetime) and isinstance(field, str):
        try:
            return parse_time(field)
        except ValueError:
            return None
    if isinstance(ref, float) and isinstance(field, (int, float)) and not isinstance(field, bool):
        return float(field)
    return field


def _predicate(pred: dict) -> Pred:
    path = pred.get("fieldPath") or ""
    comparator = pred.get("comparator", "COMPARATOR_INVALID")
    value = pred.get("value") or {}
    if comparator == "COMPARATOR_MATCH_ALL":
        return lambda _obj: True
    if not path:
        raise FilterError("predicate.fieldPath must be set")
    if comparator == "COMPARATOR_EXISTS":
        return lambda obj: _lookup(obj, path) is not None
    if comparator in ("COMPARATOR_IN", "COMPARATOR_CASE_INSENSITIVE_EQUALITY_IN"):
        items = [_scalar(v) for v in (value.get("listType") or {}).get("values", [])]
        if comparator == "COMPARATOR_CASE_INSENSITIVE_EQUALITY_IN":
            lowered = {str(i).lower() for i in items}
            return lambda obj: isinstance(_lookup(obj, path), str) and _lookup(obj, path).lower() in lowered
        return lambda obj: any(_coerce(_lookup(obj, path), i) == i for i in items)
    if comparator == "COMPARATOR_RANGE_CLOSED":
        rng = value.get("rangeType") or {}
        lo = _scalar({"numericType": rng.get("start", {})})
        hi = _scalar({"numericType": rng.get("end", {})})

        def in_range(obj: Any) -> bool:
            field = _coerce(_lookup(obj, path), 0.0)
            return isinstance(field, float) and lo <= field <= hi

        return in_range
    if comparator == "COMPARATOR_WITHIN":
        raise FilterError("COMPARATOR_WITHIN is not supported by the harness")
    ref = _scalar(value)
    if comparator == "COMPARATOR_EQUALITY":
        return lambda obj: _coerce(_lookup(obj, path), ref) == ref
    if comparator == "COMPARATOR_CASE_INSENSITIVE_EQUALITY":
        target = str(ref).lower()
        return lambda obj: isinstance(_lookup(obj, path), str) and _lookup(obj, path).lower() == target
    ops: dict[str, Callable[[Any, Any], bool]] = {
        "COMPARATOR_LESS_THAN": lambda a, b: a < b,
        "COMPARATOR_GREATER_THAN": lambda a, b: a > b,
        "COMPARATOR_LESS_THAN_EQUAL_TO": lambda a, b: a <= b,
        "COMPARATOR_GREATER_THAN_EQUAL_TO": lambda a, b: a >= b,
    }
    if comparator in ops:
        op = ops[comparator]

        def compare(obj: Any) -> bool:
            field = _coerce(_lookup(obj, path), ref)
            if field is None or isinstance(field, bool) or type(field) is not type(ref):
                return False
            return op(field, ref)

        return compare
    raise FilterError(f"unsupported comparator {comparator}")


def _children(op: dict) -> list[Pred]:
    preds = [_predicate(p) for p in (op.get("predicateSet") or {}).get("predicates", [])]
    stmts = [compile_statement(s) for s in (op.get("statementSet") or {}).get("statements", [])]
    return preds + stmts


def compile_statement(stmt: dict) -> Pred:
    """Compile a ``Statement`` into a predicate over an entity (or list member) JSON object."""
    if "and" in stmt:
        children = _children(stmt["and"])
        return lambda obj: all(c(obj) for c in children)
    if "or" in stmt:
        children = _children(stmt["or"])
        return lambda obj: any(c(obj) for c in children)
    if "not" in stmt:
        op = stmt["not"]
        if "predicate" in op:
            inner = _predicate(op["predicate"])
        elif "statement" in op:
            inner = compile_statement(op["statement"])
        else:
            raise FilterError("not operation needs predicate or statement")
        return lambda obj: not inner(obj)
    if "predicate" in stmt:
        return _predicate(stmt["predicate"])
    if "list" in stmt:
        op = stmt["list"]
        if op.get("listComparator") != "LIST_COMPARATOR_ANY_OF":
            raise FilterError("list operation requires listComparator LIST_COMPARATOR_ANY_OF")
        list_path = op.get("listPath") or ""
        inner = compile_statement(op.get("statement") or {})

        def any_of(obj: Any) -> bool:
            items = _lookup(obj, list_path)
            return isinstance(items, list) and any(inner(i) for i in items)

        return any_of
    raise FilterError("empty filter statement")
