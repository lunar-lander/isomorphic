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


@pytest.fixture
def app():
    return _load_example_app()


@pytest.fixture
def cli(app):
    return FastAPICLI(app, label="demo")


@pytest.fixture
def runner():
    return CliRunner()


def _json(out):
    return json.loads(out.stdout)


# --- demo app tests ---

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
    pid = next(p for p in get.params if p.kind == ParamKind.PATH)
    assert pid.name == "item_id"
    assert pid.required is True
    xtok = next(p for p in get.params if p.kind == ParamKind.HEADER)
    assert xtok.cli_name == "x-token"
    lim = next(p for p in get.params if p.name == "limit")
    assert lim.default == 10
    assert lim.required is False


def test_post_body_is_flattened(cli):
    post = next(r for r in cli.routes if "POST" in r.methods)
    body_fields = [p for p in post.params if p.kind == ParamKind.BODY_FIELD]
    names = {p.name for p in body_fields}
    # body fields are prefixed with the model kwarg name to avoid collisions
    assert {"item_name", "item_price", "item_tags", "filter_q"} == names
    price = next(p for p in body_fields if p.name == "item_price")
    assert price.model_name == "item"
    tags = next(p for p in body_fields if p.name == "item_tags")
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
            "--item.name", "Widget",
            "--item.price-alias", "9.99",
            "--item.tags", "a",
            "--item.tags", "b",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["item_id"] == 3
    assert out["item"] == {"name": "Widget", "priceAlias": 9.99, "tags": ["a", "b"]}
    assert out["filter"] == {"q": ""}  # filter falls back to default


def test_post_command_can_supply_filter_field(runner, cli):
    result = runner.invoke(
        cli.typer_app,
        ["post", "items-item-id", "3", "--item.name", "Widget", "--filter.q", "search"],
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


# --- from_import / _load_app ---

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


def test_load_app_rejects_empty_path():
    from fastapi_isomorphic.core import _load_app
    with pytest.raises(ValueError, match="empty"):
        _load_app("")


def test_load_app_rejects_missing_module():
    from fastapi_isomorphic.core import _load_app
    with pytest.raises(ValueError, match="module name before"):
        _load_app(":app")


def test_load_app_rejects_missing_attr():
    from fastapi_isomorphic.core import _load_app
    with pytest.raises(ValueError, match="attribute after"):
        _load_app("os.path:")


def test_load_app_rejects_multiple_colons():
    from fastapi_isomorphic.core import _load_app
    with pytest.raises(ValueError, match="multiple ':'"):
        _load_app("foo:bar:baz")


def test_load_app_attribute_error_lists_attrs(tmp_path, monkeypatch):
    pkg = tmp_path / "badpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    from fastapi_isomorphic.core import _load_app
    with pytest.raises(AttributeError, match="no attribute"):
        _load_app("badpkg:nonexistent")


# --- nested JSON body + same URL different methods ---

from pydantic import BaseModel as _BM, Field as _Field
from fastapi import FastAPI as _FA


class _Address(_BM):
    street: str
    zip: str


class _User(_BM):
    name: str
    age: int = 0
    address: _Address
    tags: list[str] = []
    secondary: _Address | None = None


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


@pytest.fixture
def nested_cli():
    return FastAPICLI(_build_nested_app(), label="nested")


def test_same_url_different_methods_get_distinct_commands(nested_cli):
    groups = {r.group for r in nested_cli.routes}
    assert {"get", "post", "put"} == groups
    cmds = {(r.group, r.command_name) for r in nested_cli.routes}
    assert ("get", "users-uid") in cmds
    assert ("post", "users-uid") in cmds
    assert ("put", "users-uid") in cmds


def test_nested_body_field_flattened_with_dots(nested_cli):
    """Nested BaseModel fields are flattened to dotted flags, prefixed by model name."""
    post = next(r for r in nested_cli.routes if r.group == "post")
    names = {p.name for p in post.params}
    assert "user_address_street" in names
    assert "user_address_zip" in names
    assert "user_secondary_street" in names
    assert "user_secondary_zip" in names
    assert "user_name" in names
    assert "user_age" in names


def test_nested_body_dotted_flags_run(runner, nested_cli):
    """--user.address.street / --user.address.zip build the nested model in-process."""
    result = runner.invoke(
        nested_cli.typer_app,
        [
            "post", "users-uid", "42",
            "--user.name", "Ada",
            "--user.age", "30",
            "--user.address.street", "1 Main",
            "--user.address.zip", "00000",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["uid"] == 42
    assert out["user"]["address"] == {"street": "1 Main", "zip": "00000"}
    assert out["user"]["secondary"] is None


def test_nested_body_optional_model_can_be_supplied(runner, nested_cli):
    result = runner.invoke(
        nested_cli.typer_app,
        [
            "post", "users-uid", "1",
            "--user.name", "Bob",
            "--user.address.street", "X",
            "--user.address.zip", "1",
            "--user.secondary.street", "Y",
            "--user.secondary.zip", "2",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["user"]["secondary"] == {"street": "Y", "zip": "2"}


def test_deeply_nested_body_flattened(runner):
    """3-level nesting: User -> Profile -> Settings -> theme."""
    cli = FastAPICLI(_build_deep_app(), label="deep")
    post = next(r for r in cli.routes if r.group == "post")
    names = {p.name for p in post.params}
    assert "user_profile_nickname" in names
    assert "user_profile_settings_theme" in names
    assert "user_profile_settings_lang" in names

    result = runner.invoke(
        cli.typer_app,
        [
            "post", "users-uid", "9",
            "--user.name", "Deep",
            "--user.profile.nickname", "dee",
            "--user.profile.settings.theme", "dark",
            "--user.profile.settings.lang", "fr",
        ],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["user"]["profile"]["settings"] == {"theme": "dark", "lang": "fr"}
    assert out["user"]["profile"]["nickname"] == "dee"


def test_same_url_get_vs_put_both_run(runner, nested_cli):
    g = runner.invoke(nested_cli.typer_app, ["get", "users-uid", "7", "--detail"])
    assert g.exit_code == 0, g.stdout
    assert _json(g) == {"uid": 7, "detail": True}

    p = runner.invoke(
        nested_cli.typer_app,
        ["put", "users-uid", "7", "--user.name", "Z", "--user.address.street", "s", "--user.address.zip", "z"],
    )
    assert p.exit_code == 0, p.stdout
    pj = _json(p)
    assert pj["method"] == "PUT"
    assert pj["user"]["name"] == "Z"


# --- Depends() handling ---

def test_depends_function_param_is_skipped_with_warning():
    """Routes with Depends() as function params should be skipped, not crash."""
    import warnings
    from fastapi import Depends

    def get_db():
        return {"db": "connected"}

    a = _FA(title="DependsApp")

    @a.get("/items/{item_id}")
    async def get_item(item_id: int, db=Depends(get_db)):
        return {"item_id": item_id, "db": db}

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cli = FastAPICLI(a, label="dep")
    assert any("Depends" in str(wi.message) for wi in w)
    assert len(cli.routes) == 0  # the route was skipped


def test_route_level_depends_is_not_skipped():
    """Route-level dependencies=[Depends(...)] are safe for CLI — not skipped."""
    import warnings
    from fastapi import Depends

    def verify_token():
        return "ok"

    a = _FA(title="RouteDep")

    @a.get("/secure/{x}", dependencies=[Depends(verify_token)])
    async def secure(x: int):
        return {"x": x}

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cli = FastAPICLI(a, label="rdep")
    # no Depends warning — route-level deps don't affect the function signature
    assert not any("Depends" in str(wi.message) for wi in w)
    assert len(cli.routes) == 1  # route was NOT skipped


# --- enum body fields ---

from enum import Enum

class _Color(str, Enum):
    red = "red"
    green = "green"
    blue = "blue"

class _Paint(_BM):
    name: str
    color: _Color


def test_enum_body_field(runner):
    """Enum fields in body models should be coercible from string."""
    a = _FA()
    @a.post("/paints/{pid}")
    async def create_paint(pid: int, paint: _Paint):
        return {"pid": pid, "paint": paint.model_dump()}

    cli = FastAPICLI(a, label="enum")
    result = runner.invoke(
        cli.typer_app,
        ["post", "paints-pid", "1", "--paint.name", "Sky", "--paint.color", "blue"],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["paint"]["color"] == "blue"


# --- union types beyond T | None ---

class _Metric(_BM):
    key: str
    value: str | int


def test_union_body_field(runner):
    """Union (str | int) fields should be coerced by pydantic.

    Note: Typer passes CLI args as strings, so pydantic validates the
    string against the union. ``str | int`` will match ``str`` first (the
    raw string "42"), so the value stays a string. This is a known
    limitation of CLI-based union handling — the user can pass JSON via
    a list[BaseModel] flag if they need exact type control.
    """
    a = _FA()
    @a.post("/metrics/{mid}")
    async def add_metric(mid: int, metric: _Metric):
        return {"mid": mid, "metric": metric.model_dump(mode="json")}

    cli = FastAPICLI(a, label="union")
    result = runner.invoke(
        cli.typer_app,
        ["post", "metrics-mid", "1", "--metric.key", "cpu", "--metric.value", "42"],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    # str | int validates the raw "42" string as str (first match)
    assert out["metric"]["value"] == "42"

    result = runner.invoke(
        cli.typer_app,
        ["post", "metrics-mid", "1", "--metric.key", "host", "--metric.value", "srv1"],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["metric"]["value"] == "srv1"


# --- routes with no parameters ---

def test_no_param_route(runner):
    """A route with no params (path/query/body) should still work."""
    a = _FA()
    @a.get("/health")
    async def health():
        return {"status": "ok"}

    cli = FastAPICLI(a, label="health")
    result = runner.invoke(cli.typer_app, ["get", "health"])
    assert result.exit_code == 0, result.stdout
    assert _json(result) == {"status": "ok"}


# --- body model field name collisions ---

class _Author(_BM):
    name: str

class _Book(_BM):
    name: str


def test_body_model_field_name_collisions(runner):
    """Two body models with same-named fields should get distinct prefixed flags."""
    a = _FA()
    @a.post("/authors/{aid}/books/{bid}")
    async def create(aid: int, bid: int, author: _Author, book: _Book):
        return {"aid": aid, "bid": bid, "author": author.model_dump(), "book": book.model_dump()}

    cli = FastAPICLI(a, label="collision")
    post = next(r for r in cli.routes if r.group == "post")
    flags = {p.cli_name for p in post.params if p.kind == ParamKind.BODY_FIELD}
    assert "author.name" in flags
    assert "book.name" in flags
    assert "author.name" != "book.name"  # no collision

    result = runner.invoke(
        cli.typer_app,
        ["post", "authors-aid-books-bid", "1", "2", "--author.name", "Alice", "--book.name", "Go"],
    )
    assert result.exit_code == 0, result.stdout
    out = _json(result)
    assert out["author"]["name"] == "Alice"
    assert out["book"]["name"] == "Go"


# --- flag uniqueness validation ---

def test_flag_uniqueness_collision_raises():
    """If two flags would collide, resolve should raise ValueError."""
    from fastapi_isomorphic.resolver import _check_uniqueness
    from fastapi_isomorphic.models import Param, ParamKind

    params = [
        Param(name="a", cli_name="x", kind=ParamKind.QUERY, annotation=str, required=False),
        Param(name="b", cli_name="x", kind=ParamKind.QUERY, annotation=str, required=False),
    ]
    with pytest.raises(ValueError, match="CLI flag collision"):
        _check_uniqueness(params, "/test")


# --- multi-method api_route ---

def test_multi_method_api_route_warns():
    """@app.api_route with multiple methods should warn but still resolve."""
    import warnings
    a = _FA()
    @a.api_route("/multi", methods=["GET", "POST"])
    async def multi():
        return {"ok": True}

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cli = FastAPICLI(a, label="multi")
    assert any("multiple methods" in str(wi.message) for wi in w)
    assert len(cli.routes) == 1
    assert cli.routes[0].group == "get"  # alphabetically first


# --- async loop fallback ---

def test_run_coroutine_no_running_loop():
    """_run_coroutine works when no event loop is running (normal CLI)."""
    import asyncio
    from fastapi_isomorphic.cli import _run_coroutine

    async def coro():
        await asyncio.sleep(0)
        return 42

    assert _run_coroutine(coro()) == 42


def test_run_coroutine_with_running_loop():
    """_run_coroutine handles a running event loop via fallback."""
    import asyncio
    from fastapi_isomorphic.cli import _run_coroutine

    async def inner():
        # This coroutine is awaited from within a running loop
        async def coro():
            await asyncio.sleep(0)
            return 99
        return _run_coroutine(coro())

    result = asyncio.run(inner())
    assert result == 99


def test_run_coroutine_thread_fallback_propagates_context():
    """Thread fallback should propagate contextvars."""
    import asyncio
    import contextvars
    from fastapi_isomorphic.cli import _run_coroutine

    var = contextvars.ContextVar("test_var", default="unset")
    var.set("from-caller")

    async def coro():
        return var.get()

    async def inner():
        return _run_coroutine(coro())

    result = asyncio.run(inner())
    assert result == "from-caller"
