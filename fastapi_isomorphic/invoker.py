"""Rebuild endpoint kwargs from flat CLI values (body models reassembled)."""
from __future__ import annotations

import inspect
import typing
from typing import Any, Dict, Optional

from pydantic import BaseModel, TypeAdapter

from .models import Param, ParamKind, ResolvedRoute


def _coerce(value: Any, annotation: Any) -> Any:
    """Coerce a raw string (from argv) to the param's python type.

    Pydantic's TypeAdapter handles scalars, lists, Optional, nested
    BaseModels and enums uniformly. ``None`` passes through untouched.
    """
    if value is None:
        return None
    if annotation is inspect.Parameter.empty or annotation is None:
        return value
    ta = TypeAdapter(annotation)
    return ta.validate_python(value)


def rebuild_args(route: ResolvedRoute, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map flat ``{param_name: raw_value}`` to endpoint kwargs.

    Path/query/header/cookie params map 1:1 by name. Body fields are
    reassembled: all fields tagged with the same ``model_name`` become a
    single pydantic model instance passed under that kwarg name. A body
    field that was supplied as ``None`` and is *optional* is dropped so the
    model's own default applies. Nested-model body fields are validated via
    the nested model (TypeAdapter coerces a JSON string or dict).
    """
    kwargs: Dict[str, Any] = {}
    # collect body fields per model
    body_buckets: Dict[str, Dict[str, Any]] = {m: {} for m in route.body_models}

    for p in route.params:
        v = raw.get(p.name, p.default)
        # path/query/header/cookie
        if p.kind in (ParamKind.PATH, ParamKind.QUERY, ParamKind.HEADER, ParamKind.COOKIE):
            kwargs[p.name] = _coerce(v, p.annotation)
            continue
        # body field -> bucket
        if p.model_name is None:
            # raw non-pydantic body
            kwargs[p.name] = _coerce(v, p.annotation)
            continue
        bucket = body_buckets[p.model_name]
        # only include the field if user supplied it (key in raw) OR it's required
        if p.name in raw or p.required:
            # validate using the wire name (alias) the model accepts
            key = p.wire_name or p.name
            bucket[key] = _coerce(v, p.annotation)

    # rebuild each body model
    hints = typing.get_type_hints(route.endpoint, include_extras=True)
    for mname in route.body_models:
        annotation = hints.get(mname, Any)
        annotation = _unwrap_optional(annotation)
        bucket = body_buckets[mname]
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            # only construct with provided fields; pydantic fills defaults
            provided = {k: v for k, v in bucket.items() if v is not None or k in raw}
            kwargs[mname] = annotation.model_validate(provided)
        else:
            kwargs[mname] = _coerce(bucket.get("__raw__"), annotation) if bucket else None

    return kwargs


def _unwrap_optional(tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, getattr(__import__("typing"), "UnionType", None)):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp
