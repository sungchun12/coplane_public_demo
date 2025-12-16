from planar.modeling.mixins import TimestampMixin
from planar.modeling.orm import Field, PlanarBaseEntity
from typing import Optional
from datetime import datetime

# TimestampMixin adds the id, created_at, updated_at, created_by, updated_by fields to the entity
# table=True makes the entity a table in the database
class Invoice(PlanarBaseEntity, TimestampMixin, table=True):
    """Invoice entity"""

    __tablename__ = "invoice"

    vendor: str = Field()
    amount: float = Field()
    invoice_number: str = Field()


class ComplexInvoice(PlanarBaseEntity, TimestampMixin, table=True):
    """Complex invoice entity for subcontractor invoice processing"""

    __tablename__ = "complex_invoice"

    vendor: str = Field()
    company: str = Field()
    invoice_date: datetime = Field()
    invoice_amount: float = Field()
    terms: str = Field()
    invoice_currency: str = Field(default="USD")
    voucher_amount: float = Field()
    pay_date: Optional[datetime] = Field(default=None)
    apply_date: Optional[datetime] = Field(default=None)
    due_date: Optional[datetime] = Field(default=None)
    discount_date: Optional[datetime] = Field(default=None)
    discount: Optional[float] = Field(default=None)
    status: str = Field(default="pending")
    approved: bool = Field(default=False)
    batch_id: Optional[str] = Field(default=None)