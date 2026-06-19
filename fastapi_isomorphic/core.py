"""High-level FastAPICLI: turn a FastAPI app into a runnable CLI."""
from __future__ import annotations

import importlib
import sys
from typing import List, Optional, Tuple

import typer
from fastapi import FastAPI

from .cli import build_cli
from .resolver import resolve_app
from .models import ResolvedRoute


def _load_app(import_path: str) -> Tuple[FastAPI, str]:
    """Import ``"module.sub:app_attr"`` -> (FastAPI instance, label).

    The label is the app attribute name (or the module's basename) used as
    the CLI's display name and command-group prefix.
    """
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    else:
        module_name, attr = import_path, "app"
    module = importlib.import_module(module_name)
    app_obj = getattr(module, attr)
    if not isinstance(app_obj, FastAPI):
        # allow a factory function that returns a FastAPI
        if callable(app_obj):
            app_obj = app_obj()
        if not isinstance(app_obj, FastAPI):
            raise TypeError(f"{import_path} did not resolve to a FastAPI instance")
    label = attr or module_name.rsplit(".", 1)[-1]
    return app_obj, label


class FastAPICLI:
    """Builder that resolves a FastAPI app into a Typer CLI.

    Usage::

        from fastapi import FastAPI
        from fastapi_isomorphic import FastAPICLI

        app = FastAPI()
        @app.get("/items/{item_id}")
        async def get_item(item_id: int): ...

        cli = FastAPICLI(app)
        cli.run()            # parse sys.argv and dispatch in-process
        cli.routes          # list[ResolvedRoute] for inspection
        cli.typer_app        # the underlying Typer instance
    """

    def __init__(self, app: FastAPI, label: Optional[str] = None):
        self.app = app
        self.label = label or app.title or "app"
        self.routes: List[ResolvedRoute] = resolve_app(app)
        self.typer_app = build_cli(self.routes, app_label=self.label)

    def run(self, args: Optional[List[str]] = None) -> None:
        """Dispatch ``args`` (default ``sys.argv[1:]``) against the CLI."""
        self.typer_app(args=args, standalone_mode=False)

    @classmethod
    def from_import(cls, import_path: str) -> "FastAPICLI":
        """Create a FastAPICLI by importing ``"pkg.mod:app"``."""
        app_obj, label = _load_app(import_path)
        return cls(app_obj, label=label)


def main() -> None:
    """Console entrypoint: ``python -m fastapi_isomorphic pkg.mod:app [args]``."""
    if len(sys.argv) < 2:
        typer.echo("usage: python -m fastapi_isomorphic <module:app> [cli args...]", err=True)
        raise SystemExit(2)
    import_path = sys.argv[1]
    rest = sys.argv[2:]
    cli = FastAPICLI.from_import(import_path)
    cli.run(rest)
