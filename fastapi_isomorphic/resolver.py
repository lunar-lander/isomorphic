"""Schema discovery and resolution from a FastAPI app into ResolvedRoutes."""
from __future__ import annotations

import inspect
import re
import typing
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from .models import Param, ParamKind, ResolvedRoute

_SENTINELS = (
    inspect.Parameter.empty,
    inspect._empty,  # type: ignore[attr-defined]
)


def _snake(s: str) -> str:
    """Normalize any string to a CLI-friendly lower_snake_case flag.

    ``item_id`` -> ``item-id``; ``x-token`` -> ``x-token``; ``Item`` -> ``item``.
    """
    s = s.strip()
    # already-kebab (http header style) stays as-is once lowercased
    if "-" in s:
        return s.lower()
    # camelCase / PascalCase -> snake
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", s)
    s = re.sub(r"_+", "_", s)
    return s.lower().replace("_", "-")


def _command_name(path: str) -> str:
    """Flatten a route path to a CLI command name.

    ``/users/{user_id}/items/{item_id}`` -> ``users-user_id-items-item-id``.
    Leading/trailing slashes and braces are dropped; path params keep their
    name so the command is self-describing and stable across route changes.
    """
    parts: List[str] = []
    for raw in path.strip("/").split("/"):
        if not raw:
            continue
        if raw.startswith("{") and raw.endswith("}"):
            parts.append(raw[1:-1].replace("_", "-"))
        else:
            parts.append(raw)
    return "-".join(parts)


def _is_list(tp: Any) -> bool:
    origin = typing.get_origin(tp)
    return origin in (list, List, typing.List)  # type: ignore[comparison-overlap]


def _list_inner(tp: Any) -> Any:
    args = typing.get_args(tp)
    return args[0] if args else str


def _unwrap_optional(tp: Any) -> Any:
    """``str | None`` -> ``str`` (None-ness handled via ``required`` flag)."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, getattr(__import__("typing"), "UnionType", None)):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _flatten_model(model: type[BaseModel], endpoint_kwarg: str, out: List[Param]) -> None:
    """Emit one :class:`Param` (kind BODY_FIELD) per leaf field of a body model.

    Nested BaseModel fields recurse using dotted model names so callers can
    rebuild nested structures. List[BaseModel] becomes a repeatable flag whose
    value is JSON per element. Enum / scalar list fields keep their scalar
    type with is_list=True so Typer collects repeats.
    """
    for fname, finfo in model.model_fields.items():
        annotation = finfo.annotation
        annotation = _unwrap_optional(annotation)
        is_list = _is_list(annotation)
        inner = _list_inner(annotation) if is_list else annotation
        default = finfo.get_default()
        from pydantic_core import PydanticUndefined
        required = finfo.is_required() and default in (None, PydanticUndefined, inspect.Parameter.empty)
        # treat a pydantic-undefined default as "no default" -> None
        if default is PydanticUndefined or default is inspect.Parameter.empty:
            default = None
        # mirror the API: prefer the field's wire alias (kebab-cased) so the
        # CLI flag matches the JSON key the endpoint actually accepts.
        alias = finfo.alias or finfo.serialization_alias or finfo.validation_alias
        cli_name = _snake(alias) if alias and alias != fname else _snake(fname)
        wire_name = alias if alias and alias != fname else fname
        # nested model
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            # keep the scalar shape (the user passes JSON for nested) but tag the
            # model_name so the rebuilder can validate via the nested model.
            out.append(
                Param(
                    name=fname,
                    cli_name=cli_name,
                    kind=ParamKind.BODY_FIELD,
                    annotation=inner if not is_list else List[inner],  # type: ignore[valid-type]
                    required=required,
                    default=default,
                    model_name=endpoint_kwarg,
                    is_list=is_list,
                    wire_name=wire_name,
                )
            )
            continue
        # plain field
        out.append(
            Param(
                name=fname,
                cli_name=cli_name,
                kind=ParamKind.BODY_FIELD,
                annotation=inner if not is_list else List[inner],  # type: ignore[valid-type]
                required=required,
                default=default,
                model_name=endpoint_kwarg,
                is_list=is_list,
                wire_name=wire_name,
            )
        )


def resolve_route(route: APIRoute) -> ResolvedRoute:
    """Turn a single :class:`fastapi.routing.APIRoute` into a ResolvedRoute.

    The endpoint function's signature is the source of truth for *types*,
    while the route's ``dependent`` partitions parameters by origin
    (path/query/header/cookie/body). Body models are flattened one Param per
    field so the CLI surface is flat (``--price``, ``--tags`` instead of one
    opaque ``--item`` JSON blob).
    """
    endpoint = route.endpoint
    sig = inspect.signature(endpoint)
    hints = typing.get_type_hints(endpoint, include_extras=True)
    dependant = route.dependant

    params: List[Param] = []
    body_models: List[str] = []

    def _base(p) -> Param:
        pname = p.name
        sp = sig.parameters.get(pname)
        annotation = hints.get(pname, sp.annotation if sp else Any)
        annotation = _unwrap_optional(annotation)
        is_list = _is_list(annotation)
        inner = _list_inner(annotation) if is_list else annotation
        # actual default lives on the field_info (Query(10)/Header(None))
        fi_default = getattr(p.field_info, "default", None)
        from pydantic_core import PydanticUndefined
        if fi_default is PydanticUndefined or fi_default is inspect.Parameter.empty:
            fi_default = None
            required = True
        else:
            # an explicit default (incl. None) means the param is optional
            required = False
        return Param(
            name=pname,
            cli_name=_snake(pname),
            kind=None,  # set by caller
            annotation=inner if is_list else annotation,
            required=required,
            default=fi_default,
            is_list=is_list,
        )

    # path
    for p in dependant.path_params:
        bp = _base(p)
        bp.kind = ParamKind.PATH
        bp.required = True
        params.append(bp)
    # query
    for p in dependant.query_params:
        bp = _base(p)
        bp.kind = ParamKind.QUERY
        params.append(bp)
    # header (Header() always has alias with '-' substitution; use alias for cli_name)
    for p in dependant.header_params:
        bp = _base(p)
        bp.kind = ParamKind.HEADER
        # FastAPI Header alias is the on-the-wire name (e.g. 'x-token'); prefer it
        alias = getattr(p, "alias", None) or getattr(p.field_info, "alias", None)
        if alias and alias != p.name:
            bp.cli_name = alias.lower()
        params.append(bp)
    # cookie
    for p in dependant.cookie_params:
        bp = _base(p)
        bp.kind = ParamKind.COOKIE
        alias = getattr(p, "alias", None) or getattr(p.field_info, "alias", None)
        if alias and alias != p.name:
            bp.cli_name = alias.lower()
        params.append(bp)
    # body -> flatten each model
    for p in dependant.body_params:
        pname = p.name
        annotation = hints.get(pname, sig.parameters.get(pname).annotation if pname in sig.parameters else Any)
        annotation = _unwrap_optional(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            body_models.append(pname)
            _flatten_model(annotation, pname, params)
        else:
            # non-pydantic body (raw scalar/list) -> single BODY_FIELD param
            is_list = _is_list(annotation)
            inner = _list_inner(annotation) if is_list else annotation
            params.append(
                Param(
                    name=pname,
                    cli_name=_snake(pname),
                    kind=ParamKind.BODY_FIELD,
                    annotation=inner if is_list else annotation,
                    required=True,
                    default=None,
                    model_name=pname,
                    is_list=is_list,
                )
            )

    method = sorted(route.methods)[0].lower() if route.methods else "any"
    group = method
    cmd = _command_name(route.path)

    doc = (endpoint.__doc__ or "").strip()
    summary = doc.splitlines()[0] if doc else f"{method.upper()} {route.path}"
    description = doc if doc else f"Invoke {method.upper()} {route.path} directly in-process."

    return ResolvedRoute(
        endpoint=endpoint,
        path=route.path,
        methods=set(route.methods),
        command_name=cmd,
        group=group,
        summary=summary,
        description=description,
        params=params,
        body_models=body_models,
    )


def resolve_app(app: FastAPI) -> List[ResolvedRoute]:
    """Resolve every APIRoute on an app, ignoring mounts/websockets."""
    return [resolve_route(r) for r in app.routes if isinstance(r, APIRoute)]
