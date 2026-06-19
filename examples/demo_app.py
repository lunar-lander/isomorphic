"""Sample FastAPI app used by tests and as a usage example."""
from __future__ import annotations

from typing import Optional

from fastapi import Body, Header, Path, Query
from pydantic import BaseModel, Field

from fastapi_isomorphic import FastAPICLI


class Item(BaseModel):
    """A catalog item."""

    name: str
    price: float = Field(default=0.0, alias="priceAlias")
    tags: list[str] = []


class Filter(BaseModel):
    q: str = ""


app = None  # populated below


def build_app():
    from fastapi import FastAPI

    a = FastAPI(title="Demo")

    @a.get("/items/{item_id}")
    async def get_item(
        item_id: int = Path(..., description="The item id"),
        q: Optional[str] = None,
        limit: int = Query(10, ge=1),
        x_token: Optional[str] = Header(None),
    ):
        """Fetch one item by id with an optional query and limit."""
        return {"item_id": item_id, "q": q, "limit": limit, "x_token": x_token}

    @a.post("/items/{item_id}")
    async def create_item(
        item_id: int,
        item: Item,
        filter: Filter = Body(default=Filter()),
    ):
        """Create an item with a flattened body and filter."""
        return {
            "item_id": item_id,
            "item": item.model_dump(by_alias=True),
            "filter": filter.model_dump(),
        }

    @a.delete("/items/{item_id}")
    async def delete_item(item_id: int):
        """Delete an item."""
        return {"deleted": item_id}

    return a


app = build_app()


if __name__ == "__main__":
    cli = FastAPICLI(app, label="demo")
    cli.run()
