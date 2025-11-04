from planar.ai import Agent
from planar.files import PlanarFile
from planar.human import Human
from planar.rules.decorator import rule
from planar.workflows import step, workflow
from pydantic import BaseModel
from app.db.entities import Invoice
from planar import get_session
from sqlalchemy import select
from typing import Any, List
# Graph representation of the workflow: [process_invoice]
# [HUMAN: Input the Invoice File as a file upload] -> [AGENT: invoice_agent] -> 
# [STEP:extract_invoice] -> [STEP: maybe_approve] -> [RULE: auto_approve] -> 
# [HUMAN: human_review if auto_approve is False] -> [OUTPUT: InvoiceData] ->
# [Potential Step: write the InvoiceData to the database or general ledger like in netsuite]


#### Input and Output Type Definitions ####
# defines the output type for the invoice agent
# class Invoice(BaseModel):
#     vendor: str
#     amount: float

# rule to make sure the input only allows float values
class RuleInput(BaseModel):
    amount: float

# rule to make sure the output only allows boolean values and a reason string
class RuleOutput(BaseModel):
    approved: bool
    reason: str
#### Input and Output Type Definitions ####

#### Agent Definition ####
invoice_agent = Agent(
    name="Invoice Agent",
    model="openai:gpt-4.1",
    tools=[],
    max_turns=1,
    system_prompt="Extract vendor and amount from invoice text.",
    user_prompt="{{input}}",
    input_type=PlanarFile,
    output_type=Invoice,
)
#### Agent Definition ####

human_review = Human(
    name="Review Invoice",
    title="Review Invoice",
    input_type=Invoice,
    output_type=Invoice,
)

# TODO: create a rule to verify if the invoice is a duplicate
# run the query within the rule
# I don't think I can do this in a rule because the examples given cannot use async fuctions. I may need to ignore the rule decorator and use a step.
# @rule(description="Verify if the invoice is a duplicate")
# def verify_duplicate_invoice_rule(invoice: Invoice) -> RuleOutput:
#     session = get_session()
#     async with session.begin():
#         stmt = select(Invoice).where(Invoice.invoice_number == invoice.invoice_number)
#         historical_invoice_numbers = await session.exec(stmt)
    
#     return VerifyDuplicateInvoice(is_duplicate=invoice.invoice_number in historical_invoice_numbers, reason=f"Invoice number {invoice.invoice_number} is a duplicate")

@rule(description="Auto approve invoices under $1000")
def auto_approve(input: RuleInput) -> RuleOutput:
    return RuleOutput(approved=input.amount < 1000, reason="Amount is under $1000")

#### Step Definitions ####
# step 1
@step(display_name="Extract invoice")
async def extract_invoice(invoice_file: PlanarFile) -> Invoice:
    result = await invoice_agent(invoice_file)
    return result.output

# step 2
# TODO: create a step to verify if the invoice is a duplicate
@step(display_name="Verify if unique invoice")
async def verify_unique_invoice_step(invoice: Invoice) -> RuleOutput:
    # based on the input invoice number, query the database for an existing invoice
    # need to use SQLAlchemy to query the database
    session = get_session()
    async with session.begin():
        stmt = select(Invoice).where(Invoice.invoice_number == invoice.invoice_number)
        historical_invoice_numbers = await session.exec(stmt) # TODO: verify this result -> Historical invoice numbers: []
        print(f"Historical invoice numbers: {historical_invoice_numbers.all()}")
    if invoice.invoice_number in historical_invoice_numbers:
        return RuleOutput(approved=False, reason=f"Invoice number {invoice.invoice_number} is a duplicate")
    else:
        return RuleOutput(approved=True, reason=f"Invoice number {invoice.invoice_number} is NOT a duplicate")

# step 3
@step(display_name="Maybe approve")
async def maybe_approve(invoice: Invoice) -> Invoice:
    auto_approve_result = await auto_approve(RuleInput(amount=invoice.amount))
    if auto_approve_result.approved: # RuleOutput.approved is defined above in a pydantic class
        return invoice
    reviewed_invoice = await human_review(invoice, suggested_data=invoice)
    return reviewed_invoice.output
#### Step Definitions ####

#### Workflow Definition ####
@workflow()
async def process_invoice_with_entity(invoice_file: PlanarFile) -> Invoice:
    invoice = await extract_invoice(invoice_file)
    unique_invoice = await verify_unique_invoice_step(invoice)
    if unique_invoice.approved:
        return await maybe_approve(invoice) # this will be properly skipped if unique_invoice.approved is False
#### Workflow Definition ####

