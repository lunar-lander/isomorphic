"""Dynamic Typer command construction from ResolvedRoutes."""
from __future__ import annotations

import asyncio
import inspect
import json
import typing
from typing import Any, Callable, Dict, List

import typer

from .invoker import rebuild_args
from .models import Param, ParamKind, ResolvedRoute

_SCALARS = (int, float, bool, str, bytes)


def _typer_annotation(p: Param) -> Any:
    """The python type Typer should see on the synthesized parameter.

    Typer switches on the annotation to pick a click type and to enable
    ``multiple=True`` for lists. We therefore hand it the *real* resolved
    type whenever it is a scalar or list-of-scalar (so ``--tags a --tags b``
    collects into a list), and fall back to ``str`` for anything Typer
    cannot parse natively (BaseModel, Enum, custom classes) -- those are
    coerced later by pydantic's TypeAdapter in :mod:`invoker`.
    """
    ann = p.annotation
    if ann in _SCALARS:
        return ann
    if p.is_list:
        origin = typing.get_origin(ann)
        if origin in (list, List, typing.List):  # type: ignore[comparison-overlap]
            inner = typing.get_args(ann)[0]
            if inner in _SCALARS:
                return List[inner]  # type: ignore[valid-type]
        return str
    return str


def _run_coroutine(coro: Any) -> Any:
    """Await a coroutine, handling both no-loop and running-loop scenarios.

    ``asyncio.run`` raises if a loop is already running (e.g. inside Jupyter
    or an async host app). In that case we run the coroutine on a dedicated
    thread with its own event loop, copying the current ``contextvars``
    context so request-scoped state, database sessions, etc. propagate.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is already running — can't use asyncio.run.
    # Run in a dedicated thread with context copy.
    import concurrent.futures
    import contextvars

    ctx = contextvars.copy_context()

    def _run() -> Any:
        new_loop = asyncio.new_event_loop()
        try:
            return ctx.run(new_loop.run_until_complete, coro)
        finally:
            new_loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run).result()


def _make_command(route: ResolvedRoute, app_label: str) -> Callable[..., Any]:
    """Construct a Typer command function for a single route.

    The function's signature is synthesized from ``route.params`` so Typer
    introspects exactly the flattened surface we want on the CLI. Path
    params become positional ``typer.Argument``s; query/header/cookie and
    flattened body fields become ``typer.Option`` flags. The body gathers
    all values, calls :func:`rebuild_args`, and invokes the endpoint
    in-process (async endpoints are awaited safely).
    """
    sig_params: List[inspect.Parameter] = []
    annotations: Dict[str, Any] = {}

    for p in route.params:
        annotation = _typer_annotation(p)
        annotations[p.name] = annotation
        if p.kind == ParamKind.PATH:
            decl = typer.Argument(
                ...,
                help=f"path: {p.name}",
            )
            sig_params.append(
                inspect.Parameter(p.name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=decl, annotation=annotation)
            )
        else:
            if p.kind == ParamKind.BODY_FIELD:
                # Body fields use None as the CLI sentinel so the rebuilder
                # can omit unsupplied fields. Show the actual pydantic default
                # in the help text and suppress Typer's own (None) default.
                if p.required:
                    default_val = ...
                else:
                    default_val = None
                show_default = False
            else:
                default_val = ... if p.required else p.default
                show_default = True
            help_text = f"{p.kind.value}: {p.name}" + (
                f" (body field of {p.model_name})" if p.model_name else ""
            )
            if p.kind == ParamKind.BODY_FIELD and not p.required and p.default is not None:
                help_text += f" [default: {p.default}]"
            opt = typer.Option(default_val, f"--{p.cli_name}", help=help_text, show_default=show_default)
            sig_params.append(
                inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY, default=opt, annotation=annotation)
            )

    def _run(**kwargs) -> None:
        raw: Dict[str, Any] = {}
        for p in route.params:
            if p.name in kwargs:
                raw[p.name] = kwargs[p.name]
        rebuilt = rebuild_args(route, raw)
        result = route.endpoint(**rebuilt)
        if inspect.iscoroutine(result):
            result = _run_coroutine(result)
        if hasattr(result, "model_dump"):
            out = result.model_dump(by_alias=True, mode="json")
        elif isinstance(result, (dict, list, str, int, float, bool)) or result is None:
            out = result
        else:
            try:
                out = json.loads(json.dumps(result, default=str))
            except Exception:
                out = str(result)
        typer.echo(json.dumps(out, indent=2, default=str))

    _run.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    _run.__annotations__ = annotations
    _run.__name__ = route.command_name.replace("-", "_")
    _run.__doc__ = route.description
    return _run


def build_cli(routes: List[ResolvedRoute], app_label: str = "app") -> typer.Typer:
    """Assemble a Typer app with one command per resolved route.

    Commands are grouped under sub-apps by HTTP method (``get``, ``post``,
    ...) so the surface mirrors the API: ``myapp get items-item-id``. A
    ``list`` command prints every discovered route as a quick reference.
    """
    root = typer.Typer(help=f"CLI mirror of FastAPI app: {app_label}", no_args_is_help=True)
    groups: Dict[str, typer.Typer] = {}

    for route in routes:
        group = route.group
        if group not in groups:
            sub = typer.Typer(help=f"{group.upper()} routes", no_args_is_help=True)
            root.add_typer(sub, name=group, help=f"{group.upper()} endpoints")
            groups[group] = sub
        cmd = _make_command(route, app_label)
        groups[group].command(name=route.command_name, help=route.summary)(cmd)

    @root.command("list", help="List all discovered routes -> CLI commands.")
    def _list() -> None:
        rows = []
        for r in routes:
            row = {
                "method": sorted(r.methods)[0] if r.methods else "-",
                "path": r.path,
                "group": r.group,
                "command": f"{r.group} {r.command_name}",
                "params": [
                    {
                        "name": p.cli_name,
                        "kind": p.kind.value,
                        "required": p.required,
                        "type": getattr(p.annotation, "__name__", str(p.annotation)),
                    }
                    for p in r.params
                ],
            }
            rows.append(row)
        typer.echo(json.dumps(rows, indent=2, default=str))

    return root
