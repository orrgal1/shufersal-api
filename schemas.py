from typing import Optional, List, Any, Dict
from pydantic import BaseModel, Field, model_validator

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
    price_ils: float
    unit: str
    image_url: str
    in_stock: bool

class SearchResponse(BaseModel):
    results: List[ProductSearchItem]

class ProductDetailResponse(BaseModel):
    id: str
    name: str
    description: str
    price_ils: float
    sale_price_ils: Optional[float] = None
    unit: str
    nutrition_url: Optional[str] = None
    in_stock: bool

class CartItem(BaseModel):
    line_id: str
    product_id: str
    name: str
    qty: float
    unit_price_ils: float
    line_total_ils: float

class CartResponse(BaseModel):
    items: List[CartItem]
    subtotal_ils: float
    delivery_fee_ils: float
    total_ils: float

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

class CheckoutSubmitRequest(BaseModel):
    quote_id: str
    confirm_total_ils: float
    cvv: Optional[str] = None

class CheckoutSubmitResponse(BaseModel):
    order_id: str
    eta: str
    total_ils: float

class FeedbackRequest(BaseModel):
    message: str
    category: str = "general"
    details: Optional[Dict[str, Any]] = None

class FeedbackResponse(BaseModel):
    status: str = "received"
    feedback_id: str
    timestamp: str
