# fastapi-isomorphic

Turn any FastAPI app into an **isomorphic CLI**: the *same* route handlers
that serve HTTP also run as command-line commands, with a flat, predictable
argument surface derived straight from the route's signature.

## Why

If you already wrote a FastAPI service, you usually want to *also* be able to
drive it from a shell -- for ops, scripting, debugging, cron. Instead of
re-implementing every endpoint as a separate Typer/Click command (and
keeping the two in sync forever), `fastapi-isomorphic` reflects your app at
runtime and builds a Typer CLI whose commands map 1:1 to your routes.
Invocations call the route handler **in-process** -- no HTTP server needed.

## Install (editable)

```bash
pip install -e .
```

For use inside Jupyter notebooks or other async environments with a running
event loop, install the optional `async` extra for better coroutine handling:

```bash
pip install -e ".[async]"
```

This installs [`nest_asyncio`](https://github.com/erdewit/nest_asyncio),
which allows awaiting coroutines within an already-running loop. Without it,
a thread-based fallback is used (with `contextvars` propagation), but
`nest_asyncio` is recommended for async hosts.

## Use

### In-process, from code

```python
from fastapi import FastAPI
from fastapi_isomorphic import FastAPICLI

app = FastAPI()

@app.get("/users/{user_id}")
async def get_user(user_id: int, active: bool = True):
    return {"user_id": user_id, "active": active}

if __name__ == "__main__":
    FastAPICLI(app).run()
```

```bash
python my_app.py get users-user-id 42 --active
```

### From any installed app, without modifying it

```bash
python -m fastapi_isomorphic my_package.my_module:app get items-item-id 7 --limit 5
# or, after `pip install -e .`:
fastapi-isomorphic my_package.my_module:app get items-item-id 7 --limit 5
```

## How the API surface maps to the CLI

| FastAPI source                | CLI shape                                                |
| --- | --- |
| Path param `/items/{item_id}` | positional argument: `... 42`                            |
| Query param `q` / `limit`      | `--q`, `--limit` (type and default kept)                |
| Header param `x_token` (alias `x-token`) | `--x-token`                          |
| Cookie param                  | `--cookie-name`                                        |
| Body `item: Item` (pydantic)  | flattened to `--item.name`, `--item.price-alias`, `--item.tags` ...   |
| Body `item: Item` with nested model | recursively flattened to dotted flags: `--item.address.street`, `--item.address.zip` |

Body models are **recursively flattened** with `.` as the nesting delimiter.
Each pydantic field — at any depth — becomes its own dotted flag, prefixed
by the body model's kwarg name to avoid collisions between multiple body
models. So `POST /users/{uid}` with body `User{name, profile: Profile{nickname, settings: Settings{theme, lang}}}`
is invoked as:

```bash
post users-uid 3 \
  --user.name Ada \
  --user.profile.nickname dee \
  --user.profile.settings.theme dark \
  --user.profile.settings.lang fr
```

When two body models share field names (e.g. `author: Author{name}` and
`book: Book{name}`), the model prefix disambiguates: `--author.name` vs
`--book.name`.

Repeated `--item.tags` produce a list. Optional fields can be omitted;
pydantic fills in their defaults. Optional nested models (`Address | None`)
whose sub-fields are all omitted resolve to `None`. `list[BaseModel]` fields
are kept as a single JSON-accepting flag (e.g. `--item.children '[{...}]'`)
since a list of complex objects cannot be meaningfully flattened into scalar
flags.

### Routes with `Depends()`

Endpoints that use `Depends()` for sub-dependencies (auth, DB sessions,
pagination, etc.) **cannot be invoked via CLI** — the raw function call
would crash with a missing-argument `TypeError`. Such routes are
**skipped at resolution time** with a warning. Call them via HTTP instead.

### Multi-method routes (`@app.api_route`)

A single route registered with multiple methods (e.g.
`@app.api_route("/x", methods=["GET", "POST"])`) generates **one command**
under the alphabetically-first method group, with a warning. This is
inherently ambiguous; prefer separate `@app.get` / `@app.post` decorators.

## Output

Each command prints the endpoint's return value as pretty JSON (pydantic
models are dumped with `by_alias=True`).

## Listing discovered commands

```bash
fastapi-isomorphic demo_app:app list
```

Prints every route with its method, path, flattened params, and types.

## Limitations / scope

- `Depends(...)` sub-dependencies cannot be resolved in a CLI context.
  Routes using `Depends()` are **skipped with a warning** at resolve time,
  not crashed at invocation. Call them via HTTP.
- File uploads (`UploadFile` / `File(...)`) are not mapped to flags; pass
  them as raw values via the body.
- WebSocket routes are ignored.
- `str | int`-style unions validate as `str` (the raw CLI string) since
  pydantic matches `str` first. Use `list[BaseModel]` JSON flags if you
  need exact type control.
