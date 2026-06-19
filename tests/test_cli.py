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


# --- nested JSON body + same URL different methods ---

from pydantic import BaseModel as _BM, Field as _Field
from fastapi import FastAPI as _FA


class _Address(_BM):
    street: str
    zip: str


class _User(_BM):
    name: str
    age: int = 0
    address: _Address              # nested required model
    tags: list[str] = []
    secondary: _Address | None = None   # nested optional model


def _build_nested_app() -> _FA:
    a = _FA(title="Nested")

    @a.post("/users/{uid}")
    async def create_user(uid: int, user: _User):
        return {"uid": uid, "user": user.model_dump()}

    @a.get("/users/{uid}")
    async def get_user(uid: int, detail: bool = False):
        return {"uid": uid, "detail": detail}

    @a.put("/users/{uid}")
    async def put_user(uid: int, user: _User):
        return {"uid": uid, "user": user.model_dump(), "method": "PUT"}

    return a


class _Settings(_BM):
    theme: str = "light"
    lang: str = "en"


class _Profile(_BM):
    nickname: str = ""
    settings: _Settings


class _DeepUser(_BM):
    name: str
    profile: _Profile


def _build_deep_app() -> _FA:
    a = _FA(title="Deep")

    @a.post("/users/{uid}")
    async def make_user(uid: int, user: _DeepUser):
        return {"uid": uid, "user": user.model_dump()}

    return a


@pytest.fixture(scope="module")
def nested_app():
    return _build_nested_app()


@pytest.fixture(scope="module")
def nested_cli(nested_app):
    return FastAPICLI(nested_app, label="nested")


def test_same_url_different_methods_get_distinct_commands(nested_cli):
    """GET/POST/PUT on /users/{uid} must produce 3 separate commands."""
    groups = {r.group for r in nested_cli.routes}
    assert {"get", "post", "put"} == groups
    # same command name, different group -> no collision
    cmds = {(r.group, r.command_name) for r in nested_cli.routes}
    assert ("get", "users-uid") in cmds
    assert ("post", "users-uid") in cmds
    assert ("put", "users-uid") in cmds


def test_nested_body_field_flattened_with_dots(nested_cli):
    """Nested BaseModel fields are flattened to dotted flags, not JSON blobs."""
    post = next(r for r in nested_cli.routes if r.group == "post")
    names = {p.name for p in post.params}
    assert "address_street" in names
    assert "address_zip" in names
    assert "secondary_street" in names  # optional nested also flattened
    assert "secondary_zip" in names
    # no single 'address' JSON flag — it's been flattened
    assert "address" not in names


def test_nested_body_dotted_flags_run(runner, nested_cli):
    """--address.street / --address.zip build the nested model in-process."""
    result = runner.invoke(
        nested_cli.typer_app,
        [
            "post", "users-uid", "42",
            "--name", "Ada",
            "--age", "30",
            "--address.street", "1 Main",
            "--address.zip", "00000",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["uid"] == 42
    assert out["user"]["address"] == {"street": "1 Main", "zip": "00000"}
    assert out["user"]["secondary"] is None  # optional nested omitted


def test_nested_body_optional_model_can_be_supplied(runner, nested_cli):
    result = runner.invoke(
        nested_cli.typer_app,
        [
            "post", "users-uid", "1",
            "--name", "Bob",
            "--address.street", "X",
            "--address.zip", "1",
            "--secondary.street", "Y",
            "--secondary.zip", "2",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["user"]["secondary"] == {"street": "Y", "zip": "2"}


def test_deeply_nested_body_flattened(runner):
    """3-level nesting: User -> Profile -> Settings -> theme."""
    cli = FastAPICLI(_build_deep_app(), label="deep")

    # check dotted flags exist at all depths
    post = next(r for r in cli.routes if r.group == "post")
    names = {p.name for p in post.params}
    assert "profile_nickname" in names
    assert "profile_settings_theme" in names
    assert "profile_settings_lang" in names

    result = runner.invoke(
        cli.typer_app,
        [
            "post", "users-uid", "9",
            "--name", "Deep",
            "--profile.nickname", "dee",
            "--profile.settings.theme", "dark",
            "--profile.settings.lang", "fr",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["user"]["profile"]["settings"] == {"theme": "dark", "lang": "fr"}
    assert out["user"]["profile"]["nickname"] == "dee"


def test_same_url_get_vs_put_both_run(runner, nested_cli):
    """GET and PUT share the path but dispatch to different handlers."""
    g = runner.invoke(nested_cli.typer_app, ["get", "users-uid", "7", "--detail"])
    assert g.exit_code == 0, g.stdout
    assert _json(g) == {"uid": 7, "detail": True}

    p = runner.invoke(
        nested_cli.typer_app,
        ["put", "users-uid", "7", "--name", "Z", "--address.street", "s", "--address.zip", "z"],
    )
    assert p.exit_code == 0, p.stdout
    pj = _json(p)
    assert pj["method"] == "PUT"
    assert pj["user"]["name"] == "Z"
