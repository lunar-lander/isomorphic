"""Schema discovery and resolution from a FastAPI app into ResolvedRoutes."""
from __future__ import annotations

import inspect
import re
import typing
import warnings
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from .models import Param, ParamKind, ResolvedRoute
from .utils import is_list, list_inner, unwrap_optional


def _snake(s: str) -> str:
    """Normalize any string to a CLI-friendly lower_snake_case flag.

    ``item_id`` -> ``item-id``; ``x-token`` -> ``x-token``; ``Item`` -> ``item``.
    """
    s = s.strip()
    if "-" in s:
        return s.lower()
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


def cli_prefix(prefix: str) -> str:
    """Convert a dotted python path to a dotted CLI path (kebab per segment)."""
    if not prefix:
        return ""
    return ".".join(_snake(seg) for seg in prefix.split("."))


def _flatten_model(
    model: type[BaseModel],
    endpoint_kwarg: str,
    prefix: str,
    wire_prefix: tuple,
    out: List[Param],
    optional_parent: bool,
) -> None:
    """Emit one :class:`Param` per leaf field of a body model, recursing into
    nested BaseModels with dotted names (``address.street``).

    Nested ``list[BaseModel]`` is kept as a single JSON-accepting flag since
    a list of complex objects cannot be meaningfully flattened into scalar
    CLI flags. Scalar lists (``list[str]``) keep their repeatable flag
    behavior. ``optional_parent`` propagates optionality: when a nested
    model is ``T | None``, all its sub-fields become CLI-optional (the user
    can omit the entire sub-tree) and the rebuilder leaves the key absent so
    pydantic applies the ``None`` default.

    The ``prefix`` is the endpoint kwarg name (e.g. ``item``) so that two
    body models with same-named fields (e.g. ``item.name`` and
    ``filter.name``) don't collide.
    """
    for fname, finfo in model.model_fields.items():
        annotation = unwrap_optional(finfo.annotation)
        lst = is_list(annotation)
        inner = list_inner(annotation) if lst else annotation
        default = finfo.get_default()
        if default is PydanticUndefined or default is inspect.Parameter.empty:
            default = None
        field_required = finfo.is_required()
        alias = finfo.alias or finfo.serialization_alias or finfo.validation_alias
        wire_key = alias if alias and alias != fname else fname
        # prefix every body field with the model kwarg name to avoid collisions
        # between multiple body models that share field names
        dotted = f"{prefix}.{fname}" if prefix else fname
        py_name = dotted.replace(".", "_")
        cli_source = alias if alias and alias != fname else fname
        cli_segment = _snake(cli_source)
        cli_dotted = f"{cli_prefix(prefix)}.{cli_segment}" if prefix else cli_segment
        wire_tuple = wire_prefix + (wire_key,)

        if lst and isinstance(inner, type) and issubclass(inner, BaseModel):
            out.append(
                Param(
                    name=py_name,
                    cli_name=cli_dotted,
                    kind=ParamKind.BODY_FIELD,
                    annotation=List[inner],  # type: ignore[valid-type]
                    required=field_required and not optional_parent,
                    default=default,
                    model_name=endpoint_kwarg,
                    is_list=True,
                    wire_path=wire_tuple,
                )
            )
            continue

        if isinstance(inner, type) and issubclass(inner, BaseModel):
            _flatten_model(
                inner,
                endpoint_kwarg,
                dotted,
                wire_tuple,
                out,
                optional_parent or not field_required,
            )
            continue

        out.append(
            Param(
                name=py_name,
                cli_name=cli_dotted,
                kind=ParamKind.BODY_FIELD,
                annotation=inner if not lst else List[inner],  # type: ignore[valid-type]
                required=field_required and not optional_parent,
                default=default,
                model_name=endpoint_kwarg,
                is_list=lst,
                wire_path=wire_tuple,
            )
        )


def _check_uniqueness(params: List[Param], path: str) -> None:
    """Raise if two non-path params share the same ``cli_name``.

    Path params are positional (no flag) so they're excluded. This check
    prevents silent data loss when two options would map to the same
    ``--flag``. Path param name collisions are not checked here — FastAPI
    itself rejects duplicate path param names at route definition time.
    """
    seen: dict[str, str] = {}
    for p in params:
        if p.kind == ParamKind.PATH:
            continue  # positional, no flag
        if p.cli_name in seen:
            raise ValueError(
                f"CLI flag collision on route {path}: --{p.cli_name} maps to "
                f"both {seen[p.cli_name]} and {p.name}. "
                f"Use distinct field/alias names or reduce body model overlap."
            )
        seen[p.cli_name] = p.name


# FastAPI injects these as named attributes on the Dependant rather than
# in the path/query/header/cookie/body param lists. They have real defaults
# or are framework-internal, so they're safe to omit when calling the raw
# endpoint function directly.
_SAFE_INJECTED_PARAM_NAMES = frozenset({
    "request", "response", "background_tasks", "scope",
    "http_connection", "websocket", "security_scopes",
})


def _has_unresolvable_dependencies(route: APIRoute) -> bool:
    """True if the endpoint has Depends() *function params* we can't satisfy.

    FastAPI stores both route-level dependencies (``dependencies=[...]``)
    and function-parameter dependencies (``db=Depends(get_db)``) in
    ``dependant.dependencies``. Route-level deps don't appear in the
    endpoint signature, so they're safe for CLI invocation. Function-param
    deps appear in the signature but NOT in any dependant param list
    (path/query/header/cookie/body) — calling the raw function would raise
    ``TypeError: missing argument``.

    We detect the dangerous case by comparing the endpoint's signature
    parameters against the union of all resolvable param names. Any sig
    param not found there (and not a known framework-injected name) is a
    Depends() function param.
    """
    d = route.dependant
    all_resolvable = set()
    for attr in ("path_params", "query_params", "header_params", "cookie_params", "body_params"):
        for p in getattr(d, attr):
            all_resolvable.add(p.name)
    # add framework-injected names that FastAPI handles via named attributes
    for attr in ("request_param_name", "response_param_name", "background_tasks_param_name",
                 "websocket_param_name", "http_connection_param_name", "security_scopes_param_name"):
        val = getattr(d, attr, None)
        if val:
            all_resolvable.add(val)
    sig = inspect.signature(route.endpoint)
    for pname in sig.parameters:
        if pname in all_resolvable or pname in _SAFE_INJECTED_PARAM_NAMES:
            continue
        # This param is in the signature but not resolvable -> it's a Depends()
        return True
    return False


def resolve_route(route: APIRoute) -> Optional[ResolvedRoute]:
    """Turn a single :class:`fastapi.routing.APIRoute` into a ResolvedRoute.

    Returns ``None`` (with a warning) if the route has ``Depends()``
    sub-dependencies that cannot be resolved in a CLI context, since calling
    the raw endpoint would crash with a missing-argument TypeError.

    The endpoint function's signature is the source of truth for *types*,
    while the route's ``dependent`` partitions parameters by origin
    (path/query/header/cookie/body). Body models are flattened one Param per
    field so the CLI surface is flat (``--price``, ``--tags`` instead of one
    opaque ``--item`` JSON blob).
    """
    endpoint = route.endpoint
    sig = inspect.signature(endpoint)
    try:
        hints = typing.get_type_hints(endpoint, include_extras=True)
    except NameError:
        # forward refs that can't be resolved — fall back to raw annotations
        hints = {name: param.annotation for name, param in sig.parameters.items() if param.annotation is not inspect.Parameter.empty}
    dependant = route.dependant

    # Detect Depends() — can't call raw endpoint
    if _has_unresolvable_dependencies(route):
        warnings.warn(
            f"Skipping route {route.methods} {route.path}: endpoint uses "
            f"Depends() which cannot be resolved in a CLI context. "
            f"Call this route via HTTP instead.",
            stacklevel=2,
        )
        return None

    params: List[Param] = []
    body_models: List[str] = []

    def _base(p) -> Param:
        pname = p.name
        sp = sig.parameters.get(pname)
        annotation = hints.get(pname, sp.annotation if sp else Any)
        annotation = unwrap_optional(annotation)
        lst = is_list(annotation)
        inner = list_inner(annotation) if lst else annotation
        fi_default = getattr(p.field_info, "default", None)
        if fi_default is PydanticUndefined or fi_default is inspect.Parameter.empty:
            fi_default = None
            required = True
        else:
            required = False
        return Param(
            name=pname,
            cli_name=_snake(pname),
            kind=None,  # set by caller
            annotation=inner if lst else annotation,
            required=required,
            default=fi_default,
            is_list=lst,
        )

    for p in dependant.path_params:
        bp = _base(p)
        bp.kind = ParamKind.PATH
        bp.required = True
        params.append(bp)
    for p in dependant.query_params:
        bp = _base(p)
        bp.kind = ParamKind.QUERY
        params.append(bp)
    for p in dependant.header_params:
        bp = _base(p)
        bp.kind = ParamKind.HEADER
        alias = getattr(p, "alias", None) or getattr(p.field_info, "alias", None)
        if alias and alias != p.name:
            bp.cli_name = alias.lower()
        params.append(bp)
    for p in dependant.cookie_params:
        bp = _base(p)
        bp.kind = ParamKind.COOKIE
        alias = getattr(p, "alias", None) or getattr(p.field_info, "alias", None)
        if alias and alias != p.name:
            bp.cli_name = alias.lower()
        params.append(bp)
    for p in dependant.body_params:
        pname = p.name
        annotation = hints.get(pname, sig.parameters.get(pname).annotation if pname in sig.parameters else Any)
        annotation = unwrap_optional(annotation)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            body_models.append(pname)
            # prefix name/cli_name with the model kwarg for collision avoidance,
            # but keep wire_path as the pure field path (no model name) since
            # model_validate expects the model's own fields.
            _flatten_model(annotation, pname, pname, (), params, optional_parent=False)
        else:
            lst = is_list(annotation)
            inner = list_inner(annotation) if lst else annotation
            params.append(
                Param(
                    name=pname,
                    cli_name=_snake(pname),
                    kind=ParamKind.BODY_FIELD,
                    annotation=inner if lst else annotation,
                    required=True,
                    default=None,
                    model_name=pname,
                    is_list=lst,
                    wire_path=(),
                )
            )

    _check_uniqueness(params, route.path)

    # Use the first method alphabetically as the group; warn if multiple
    methods_sorted = sorted(route.methods) if route.methods else []
    method = methods_sorted[0].lower() if methods_sorted else "any"
    if len(methods_sorted) > 1:
        warnings.warn(
            f"Route {route.path} has multiple methods {methods_sorted}; "
            f"generating a single command under group '{method}'. "
            f"Use @app.api_route with caution — the command may be ambiguous.",
            stacklevel=2,
        )

    cmd = _command_name(route.path)
    doc = (endpoint.__doc__ or "").strip()
    summary = doc.splitlines()[0] if doc else f"{method.upper()} {route.path}"
    description = doc if doc else f"Invoke {method.upper()} {route.path} directly in-process."

    return ResolvedRoute(
        endpoint=endpoint,
        path=route.path,
        methods=set(route.methods),
        command_name=cmd,
        group=method,
        summary=summary,
        description=description,
        params=params,
        body_models=body_models,
    )


def resolve_app(app: FastAPI) -> List[ResolvedRoute]:
    """Resolve every APIRoute on an app, ignoring mounts/websockets.

    Routes with unresolvable ``Depends()`` are skipped with a warning.
    """
    resolved: List[ResolvedRoute] = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            rr = resolve_route(r)
            if rr is not None:
                resolved.append(rr)
    return resolved
