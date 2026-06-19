"""End-to-end tests for fastapi-isomorphic."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Make the repo root importable when running pytest from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi_isomorphic import FastAPICLI
from fastapi_isomorphic.models import ParamKind
from fastapi_isomorphic.resolver import resolve_app

import importlib.util

EXAMPLE_PATH = ROOT / "examples" / "demo_app.py"


def _load_example_app():
    spec = importlib.util.spec_from_file_location("demo_app", EXAMPLE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.app


@pytest.fixture(scope="module")
def app():
    return _load_example_app()


@pytest.fixture(scope="module")
def cli(app):
    return FastAPICLI(app, label="demo")


@pytest.fixture(scope="module")
def runner():
    return CliRunner()


def _json(out: str):
    return json.loads(out.stdout)


def test_resolver_counts_routes(app):
    routes = resolve_app(app)
    methods = sorted(r.methods for r in routes)
    assert {frozenset(m) for m in methods} == {frozenset({"GET"}), frozenset({"POST"}), frozenset({"DELETE"})}


def test_get_item_params_flattened(cli):
    get = next(r for r in cli.routes if "GET" in r.methods)
    kinds = {p.kind for p in get.params}
    assert ParamKind.PATH in kinds
    assert ParamKind.QUERY in kinds
    assert ParamKind.HEADER in kinds
    # path positional
    pid = next(p for p in get.params if p.kind == ParamKind.PATH)
    assert pid.name == "item_id"
    assert pid.required is True
    # header uses wire alias
    xtok = next(p for p in get.params if p.kind == ParamKind.HEADER)
    assert xtok.cli_name == "x-token"
    # query default preserved
    lim = next(p for p in get.params if p.name == "limit")
    assert lim.default == 10
    assert lim.required is False


def test_post_body_is_flattened(cli):
    post = next(r for r in cli.routes if "POST" in r.methods)
    body_fields = [p for p in post.params if p.kind == ParamKind.BODY_FIELD]
    names = {p.name for p in body_fields}
    assert {"name", "price", "tags", "q"}.issubset(names)
    # price keeps its alias on the wire side -- the python field name is `price`
    price = next(p for p in body_fields if p.name == "price")
    assert price.model_name == "item"
    # tags is a list
    tags = next(p for p in body_fields if p.name == "tags")
    assert tags.is_list is True


def test_get_command_runs_in_process(runner, cli):
    result = runner.invoke(cli.typer_app, ["get", "items-item-id", "7", "--limit", "5", "--x-token", "abc"])
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out == {"item_id": 7, "q": None, "limit": 5, "x_token": "abc"}


def test_get_command_uses_defaults_when_omitted(runner, cli):
    result = runner.invoke(cli.typer_app, ["get", "items-item-id", "1"])
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out == {"item_id": 1, "q": None, "limit": 10, "x_token": None}


def test_post_command_rebuilds_body_model(runner, cli):
    result = runner.invoke(
        cli.typer_app,
        [
            "post", "items-item-id", "3",
            "--name", "Widget",
            "--price-alias", "9.99",
            "--tags", "a",
            "--tags", "b",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["item_id"] == 3
    assert out["item"] == {"name": "Widget", "priceAlias": 9.99, "tags": ["a", "b"]}
    # filter falls back to its default model (q="")
    assert out["filter"] == {"q": ""}


def test_post_command_can_supply_filter_field(runner, cli):
    result = runner.invoke(
        cli.typer_app,
        ["post", "items-item-id", "3", "--name", "Widget", "--q", "search"],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["filter"] == {"q": "search"}


def test_delete_command_runs(runner, cli):
    result = runner.invoke(cli.typer_app, ["delete", "items-item-id", "9"])
    assert result.exit_code == 0, result.stdout
    assert _json(result) == {"deleted": 9}


def test_list_command_lists_all_routes(runner, cli):
    result = runner.invoke(cli.typer_app, ["list"])
    assert result.exit_code == 0, result.stdout
    rows = _json(result)
    assert len(rows) == 3
    cmds = {r["command"] for r in rows}
    assert "get items-item-id" in cmds
    assert "post items-item-id" in cmds
    assert "delete items-item-id" in cmds


def test_from_import_loads_app(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "appmod.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/ping/{n}')\n"
        "async def ping(n: int):\n"
        "    return {'pong': n}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    cli = FastAPICLI.from_import("mypkg.appmod:app")
    assert any("ping" in r.command_name for r in cli.routes)
