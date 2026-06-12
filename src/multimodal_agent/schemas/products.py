"""Product search and price comparison schemas."""

from pydantic import BaseModel, Field


class ProductResult(BaseModel):
    """A candidate product returned by product search."""

    product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    price: float = Field(ge=0)
    currency: str = Field(default="CNY", min_length=1)
    platform: str = Field(min_length=1)
    url: str | None = None
    image_url: str | None = None
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    rating: float | None = Field(default=None, ge=0.0, le=5.0)
    reason: str | None = None


class PriceCompareResult(BaseModel):
    """Price comparison result for product candidates."""

    query: str = Field(min_length=1)
    items: list[ProductResult] = Field(default_factory=list)
    best_value_product_id: str | None = None
    summary: str = Field(min_length=1)
