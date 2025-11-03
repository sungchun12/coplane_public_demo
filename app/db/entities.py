from planar.modeling.mixins import TimestampMixin
from planar.modeling.orm import Field, PlanarBaseEntity


class Invoice(PlanarBaseEntity, TimestampMixin, table=True):
    """Invoice entity"""

    __tablename__ = "invoice"

    vendor: str = Field()
    amount: float = Field()