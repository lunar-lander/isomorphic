"""Enums and dataclasses describing a resolved FastAPI route."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Set, Optional


class ParamKind(str, Enum):
    """Where a CLI param originates from on the FastAPI route."""

    PATH = "path"
    QUERY = "query"
    HEADER = "header"
    COOKIE = "cookie"
    BODY_FIELD = "body_field"  # a flattened field of a pydantic body model


@dataclass
class Param:
    """A single CLI argument derived from a route parameter or body field.

    Attributes mirror what Typer needs to build a click.Parameter:

    name:       The Python kwarg name used when calling the endpoint. For
               body fields this is the *model field name* so the flattened
               option rebuilds the pydantic model correctly.
    cli_name:   The user-facing flag name (always lower_snake_case, e.g.
               ``item-id`` or ``x-token``). Path params are positional and
               never have a flag.
    kind:       Origin of the param (path/query/header/cookie/body_field).
    annotation: Resolved python type. ``list[T]`` / ``T | None`` are
               preserved so Typer can parse JSON-ish values.
    required:   True when the value must be supplied on the command line.
    default:    Default value used when not required and not supplied.
                ``None`` is a valid default for optional params.
    model_name: For BODY_FIELD params, the body model's python attribute
               name (the endpoint kwarg) so we know which model to rebuild.
    is_list:    True when the resolved type is ``list[...]`` (Typer treats
               multiple --flag repeats as a list).
    """

    name: str
    cli_name: str
    kind: ParamKind
    annotation: Any
    required: bool
    default: Any = None
    model_name: Optional[str] = None
    is_list: bool = False
    wire_name: Optional[str] = None  # JSON key the API/pydantic accepts (alias or field name)


@dataclass
class ResolvedRoute:
    """A FastAPI APIRoute fully analyzed and ready to become a CLI command."""

    endpoint: Callable[..., Any]
    path: str
    methods: Set[str]
    command_name: str
    group: str
    summary: str
    description: str
    params: List[Param] = field(default_factory=list)
    body_models: List[str] = field(default_factory=list)  # endpoint kwarg names that are body models
