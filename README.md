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
| Body `item: Item` (pydantic)  | flattened to `--name`, `--price-alias`, `--tags` ...   |
| Body `item: Item` with nested model | recursively flattened to dotted flags: `--item.address.street`, `--item.address.zip` |

Body models are **recursively flattened** with `.` as the nesting delimiter.
Each pydantic field — at any depth — becomes its own dotted flag, so
`POST /users/{uid}` with body `User{name, profile: Profile{nickname, settings: Settings{theme, lang}}}`
is invoked as:

```bash
post users-uid 3 --name Ada --profile.nickname dee --profile.settings.theme dark --profile.settings.lang fr
```

Repeated `--tags` produce a list. Optional fields can be omitted; pydantic
fills in their defaults. Optional nested models (`Address | None`) whose
sub-fields are all omitted resolve to `None`. `list[BaseModel]` fields are
kept as a single JSON-accepting flag (e.g. `--items '[{...}]'`) since a list
of complex objects cannot be meaningfully flattened into scalar flags.

## Output

Each command prints the endpoint's return value as pretty JSON (pydantic
models are dumped with `by_alias=True`).

## Listing discovered commands

```bash
fastapi-isomorphic demo_app:app list
```

Prints every route with its method, path, flattened params, and types.

## Limitations / scope

- `Depends(...)` sub-dependencies are *not* flattened yet; only direct
  path/query/header/cookie/body params of the endpoint are mirrored. (The
  endpoint still receives its `Depends`-resolved values only when run as an
  API; under CLI we call the raw function, so `Depends` params must not be
  on the mirrored endpoint. This is the same trade-off Typer would make.)
- File uploads (`UploadFile` / `File(...)`) are not mapped to flags; pass
  them as raw values via the body.
- WebSocket routes are ignored.
