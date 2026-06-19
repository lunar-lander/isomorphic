"""Shared type utilities for resolver and invoker."""
from __future__ import annotations

import typing
from typing import Any, List

try:
    from types import UnionType  # py3.10+
except ImportError:  # pragma: no cover
    UnionType = None  # type: ignore[assignment]


def is_list(tp: Any) -> bool:
    origin = typing.get_origin(tp)
    return origin in (list, List, typing.List)  # type: ignore[comparison-overlap]


def list_inner(tp: Any) -> Any:
    args = typing.get_args(tp)
    return args[0] if args else str


def unwrap_optional(tp: Any) -> Any:
    """``str | None`` -> ``str`` (None-ness handled via ``required`` flag)."""
    origin = typing.get_origin(tp)
    union_types = (typing.Union,) if UnionType is None else (typing.Union, UnionType)
    if origin in union_types:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp
