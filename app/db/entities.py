from planar.modeling.mixins import TimestampMixin
from planar.modeling.orm import Field, PlanarBaseEntity

# TimestampMixin adds the id, created_at, updated_at, created_by, updated_by fields to the entity
# table=True makes the entity a table in the database
class Invoice(PlanarBaseEntity, TimestampMixin, table=True):
    """Invoice entity"""

    __tablename__ = "invoice"

    vendor: str = Field()
    amount: float = Field()