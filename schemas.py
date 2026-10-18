from typing import Optional, List, Any, Dict
from pydantic import BaseModel, ConfigDict, Field, model_validator

class ErrorDetail(BaseModel):
    code: str
    message: str

class ErrorResponse(BaseModel):
    error: ErrorDetail

class HealthResponse(BaseModel):
    status: str = "ok"
    logged_in: bool
    cart_size: int

class ProductSearchItem(BaseModel):
    id: str
    name: str
    brand: str
    # The site's shelf price; falls back to its effective/range price, and is
    # null only when the site sends no price at all for the product.
    price_ils: Optional[float] = None
    sale_price_ils: Optional[float] = None
    price_per_unit_ils: Optional[float] = None
    selling_method: Optional[str] = None
    unit: str
    image_url: str
    in_stock: bool

class SearchResponse(BaseModel):
    results: List[ProductSearchItem]

class ProductDetailResponse(BaseModel):
    id: str
    name: str
    description: str
    price_ils: Optional[float] = None
    sale_price_ils: Optional[float] = None
    unit: str
    nutrition_url: Optional[str] = None
    in_stock: bool

class CartItem(BaseModel):
    line_id: str
    product_id: str
    name: str
    qty: float
    selling_method: Optional[str] = None
    # None when the site price could not be read; never a silent 0.
    unit_price_ils: Optional[float] = None
    line_total_ils: Optional[float] = None

class CartResponse(BaseModel):
    items: List[CartItem]
    item_count: Optional[int] = None
    # False when the site's item count disagrees with the lines the page showed.
    lines_complete: bool = True
    subtotal_ils: float
    # Depends on the delivery slot; None until a checkout quote picks one.
    delivery_fee_ils: Optional[float] = None
    total_ils: float
    total_includes_delivery: bool = False
    prices_complete: bool = True
    cart_hash: Optional[str] = None

class CartItemAddRequest(BaseModel):
    product_id: str
    qty: float = Field(default=1.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def allow_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "product_id" not in data:
                if "code" in data:
                    data["product_id"] = str(data["code"])
                elif "id" in data:
                    data["product_id"] = str(data["id"])
        return data

class CartItemUpdateRequest(BaseModel):
    qty: float = Field(ge=0)
    # Required: the product on the line. Line ids shift when lines are removed.
    product_code: Optional[str] = None

class CartBatchItem(BaseModel):
    product_id: str
    qty: float = Field(default=1.0, gt=0)

    @model_validator(mode="before")
    @classmethod
    def allow_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "product_id" not in data:
                if "code" in data:
                    data["product_id"] = str(data["code"])
                elif "id" in data:
                    data["product_id"] = str(data["id"])
        return data

class CartBatchRequest(BaseModel):
    items: List[CartBatchItem]
    clear_first: bool = False

class DeliverySlot(BaseModel):
    id: str
    date: str
    window: str
    fee_ils: float
    available: bool
    start: Optional[str] = None
    end: Optional[str] = None

class DeliverySlotsResponse(BaseModel):
    slots: List[DeliverySlot]

class PaymentMethodResponse(BaseModel):
    brand: str
    last4: str
    expiry_month: str
    expiry_year: str
    name_on_card: str

class CheckoutQuoteResponse(BaseModel):
    delivery_slot_id: str
    subtotal_ils: float
    delivery_fee_ils: float
    total_ils: float
    item_count: int
    quote_id: str
    expires_at: str
    cart_hash: Optional[str] = None
    items: List[Dict[str, Any]] = []
    slot: Optional[Dict[str, Any]] = None
    payment: Optional[Dict[str, Any]] = None
    prices_complete: bool = True
    total_is_estimate: bool = True

class ExpectedItem(BaseModel):
    product_id: str
    qty: float

class CheckoutSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str
    confirm_total_ils: float
    # The caller's view of the cart: the quote's cart_hash, or the item list.
    expected_cart_hash: Optional[str] = None
    expected_items: Optional[List[ExpectedItem]] = None
    dry_run: bool = True
    confirm: bool = False
    # Watched run: the checkout opens in a visible browser window; Or handles any
    # password/SMS, card and 3-D Secure step and clicks place-order himself; the
    # service returns the order number from the confirmation page.
    watched: bool = False

    @model_validator(mode="after")
    def require_expectation(self):
        if self.expected_cart_hash is None and self.expected_items is None:
            raise ValueError("expected_cart_hash or expected_items is required")
        return self

class CheckoutSubmitResponse(BaseModel):
    status: str  # dry_run | placed | placed_unconfirmed | three_ds_required | outcome_unknown | handover_timeout
    dry_run: bool
    # The site's order number, parsed from its confirmation page; never invented.
    order_id: Optional[str] = None
    total_ils: Optional[float] = None
    site_total_ils: Optional[float] = None
    summary: Dict[str, Any] = {}
    checks: Dict[str, Any] = {}
    blockers: List[str] = []
    warnings: List[str] = []

class FeedbackRequest(BaseModel):
    message: str
    category: str = "general"
    details: Optional[Dict[str, Any]] = None

class FeedbackResponse(BaseModel):
    status: str = "received"
    feedback_id: str
    timestamp: str
