from planar.ai import Agent
from planar.files import PlanarFile
from planar.human import Human
from planar.rules.decorator import rule
from planar.workflows import step, workflow
from pydantic import BaseModel


class InvoiceData(BaseModel):
    vendor: str
    amount: float


class RuleInput(BaseModel):
    amount: float


class RuleOutput(BaseModel):
    approved: bool
    reason: str


invoice_agent = Agent(
    name="Invoice Agent",
    model="openai:gpt-4.1",
    tools=[],
    max_turns=1,
    system_prompt="Extract vendor and amount from invoice text.",
    user_prompt="{{input}}",
    input_type=PlanarFile,
    output_type=InvoiceData,
)


human_review = Human(
    name="Review Invoice",
    title="Review Invoice",
    input_type=InvoiceData,
    output_type=InvoiceData,
)


@rule(description="Auto approve invoices under $1000")
def auto_approve(input: RuleInput) -> RuleOutput:
    return RuleOutput(approved=input.amount < 1000, reason="Amount is under $1000")


@step(display_name="Extract invoice")
async def extract_invoice(invoice_file: PlanarFile) -> InvoiceData:
    result = await invoice_agent(invoice_file)
    return result.output


@step(display_name="Maybe approve")
async def maybe_approve(invoice: InvoiceData) -> InvoiceData:
    auto_approve_result = await auto_approve(RuleInput(amount=invoice.amount))
    if auto_approve_result.approved:
        return invoice
    reviewed_invoice = await human_review(invoice, suggested_data=invoice)
    return reviewed_invoice.output


@workflow()
async def process_invoice(invoice_file: PlanarFile) -> InvoiceData:
    invoice = await extract_invoice(invoice_file)
    return await maybe_approve(invoice)