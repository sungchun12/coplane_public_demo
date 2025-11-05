from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
 
from app.db.entities import Invoice
from planar import get_session
 
router = APIRouter()
 
 
class InvoiceRequest(BaseModel):
    message: str
 
 
class InvoiceResponse(BaseModel):
    status: str
    echo: str

# create API endpoints based on the Invoice entity
# but why would I want to do this when I can just use the workflow directly?
# this is so that a customer's existing orchestrator can interact with the workflows
@router.post("/invoices")
async def create_invoice(data: InvoiceRequest) -> InvoiceResponse:
    session = get_session()
    async with session.begin(): # Use a transaction block, which will auto-commit at the end
        new_invoice = Invoice(**data.model_dump())
        session.add(new_invoice)
        # No need for manual commit() - the transaction block handles it
    return InvoiceResponse(status="success", echo=data.message)
 
@router.post("/invoices/{invoice_id}/approve")
async def approve_invoice(invoice_id: str) -> InvoiceResponse:
    session = get_session()
    async with session.begin(): # Use a transaction block, which will auto-commit at the end
        invoice = await session.get(Invoice, invoice_id)
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        invoice.status = "approved"
        # No need for manual commit() - the transaction block handles it
    return InvoiceResponse(status="success", echo=f"Invoice {invoice_id} approved")