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

    Raises ``ValueError`` with a helpful message for malformed inputs, and
    ``ModuleNotFoundError`` / ``AttributeError`` propagate naturally.
    """
    if not import_path or not import_path.strip():
        raise ValueError(
            "Import path must not be empty. Expected 'module.sub:app_attr' or 'module.sub'."
        )
    if ":" in import_path:
        parts = import_path.split(":", 1)
        module_name, attr = parts[0], parts[1]
        if not module_name:
            raise ValueError(
                f"Malformed import path {import_path!r}: module name before ':' is empty. "
                f"Expected 'module.sub:app_attr'."
            )
        if not attr:
            raise ValueError(
                f"Malformed import path {import_path!r}: attribute after ':' is empty. "
                f"Expected 'module.sub:app_attr'."
            )
        if ":" in attr:
            raise ValueError(
                f"Malformed import path {import_path!r}: multiple ':' found. "
                f"Expected 'module.sub:app_attr' with exactly one ':'."
            )
    else:
        module_name, attr = import_path, "app"

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            f"Could not import module {module_name!r}: {e}. "
            f"Ensure the module is installed or on PYTHONPATH."
        ) from e

    try:
        app_obj = getattr(module, attr)
    except AttributeError:
        raise AttributeError(
            f"Module {module_name!r} has no attribute {attr!r}. "
            f"Available attributes: {[a for a in dir(module) if not a.startswith('_')][:20]}..."
        ) from None

    if not isinstance(app_obj, FastAPI):
        if callable(app_obj):
            app_obj = app_obj()
        if not isinstance(app_obj, FastAPI):
            raise TypeError(
                f"{import_path!r} resolved to {type(app_obj).__name__}, not a FastAPI instance. "
                f"Pass 'module:app_attr' where app_attr is a FastAPI app or a zero-arg factory."
            )

    label = attr if attr != "app" else module_name.rsplit(".", 1)[-1]
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
