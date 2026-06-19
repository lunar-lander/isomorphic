"""Rebuild endpoint kwargs from flat CLI values (body models reassembled)."""
from __future__ import annotations

import inspect
import json
import typing
from typing import Any, Dict, List

from pydantic import BaseModel, TypeAdapter

from .models import Param, ParamKind, ResolvedRoute
from .utils import unwrap_optional

# Cache TypeAdapter instances to avoid re-building the validation schema on
# every call to _coerce.  Keyed by id(annotation) since not all annotations
# are hashable (e.g. generic aliases).
_TA_CACHE: Dict[int, TypeAdapter] = {}


def _get_type_adapter(annotation: Any) -> TypeAdapter:
    key = id(annotation)
    ta = _TA_CACHE.get(key)
    if ta is None:
        ta = TypeAdapter(annotation)
        _TA_CACHE[key] = ta
    return ta


def _coerce(value: Any, annotation: Any) -> Any:
    """Coerce a raw string (from argv) to the param's python type.

    Pydantic's TypeAdapter handles scalars, lists, Optional, nested
    BaseModels and enums uniformly. ``None`` passes through untouched.
    For BaseModel / list[BaseModel] annotations we first ``json.loads``
    the string so pydantic receives a dict/list rather than a raw string
    (it won't auto-parse JSON into a model).
    """
    if value is None:
        return None
    if annotation is inspect.Parameter.empty or annotation is None:
        return value
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    inner = args[0] if args else None
    # Pre-parse JSON strings for types that expect structured data (dicts,
    # lists, BaseModels). TypeAdapter won't auto-parse a raw JSON string
    # into these types.
    needs_json = (
        isinstance(annotation, type) and issubclass(annotation, (BaseModel, dict))
    ) or (
        origin in (list, List, typing.List, dict)  # type: ignore[comparison-overlap]
    ) or (
        annotation in (dict, list)
    )
    if needs_json and isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    ta = _get_type_adapter(annotation)
    return ta.validate_python(value)


def _set_nested(d: dict, path: tuple, value: Any) -> None:
    """Set ``value`` at a tuple path inside nested dict ``d``."""
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def rebuild_args(route: ResolvedRoute, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map flat ``{param_name: raw_value}`` to endpoint kwargs.

    Path/query/header/cookie params map 1:1 by name. Body fields are
    reassembled: all fields tagged with the same ``model_name`` are placed
    into a nested dict (using each field's ``wire_path`` tuple of alias/field
    keys) and then validated into a single pydantic model instance. Fields
    the user did not supply (value is ``None``) are omitted so pydantic
    applies the model's own defaults. Nested optional models whose sub-fields
    are all omitted simply won't appear in the dict, yielding ``None``.

    ``wire_path`` is the pure field path within the model (e.g.
    ``("address", "street")``), NOT including the model kwarg name. The
    model name is tracked separately via ``Param.model_name``.
    """
    kwargs: Dict[str, Any] = {}
    body_buckets: Dict[str, dict] = {m: {} for m in route.body_models}

    for p in route.params:
        v = raw.get(p.name, p.default)
        if p.kind in (ParamKind.PATH, ParamKind.QUERY, ParamKind.HEADER, ParamKind.COOKIE):
            kwargs[p.name] = _coerce(v, p.annotation)
            continue
        if p.model_name is None:
            kwargs[p.name] = _coerce(v, p.annotation)
            continue
        if v is None:
            continue
        coerced = _coerce(v, p.annotation)
        _set_nested(body_buckets[p.model_name], p.wire_path, coerced)

    hints = typing.get_type_hints(route.endpoint, include_extras=True)
    for mname in route.body_models:
        annotation = unwrap_optional(hints.get(mname, Any))
        bucket = body_buckets[mname]
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            kwargs[mname] = annotation.model_validate(bucket)
        else:
            kwargs[mname] = _coerce(bucket, annotation)

    return kwargs
