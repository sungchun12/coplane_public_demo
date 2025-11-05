from planar.ai import Agent
from planar.files import PlanarFile
from planar.human import Human
from planar.rules.decorator import rule
from planar.workflows import step, workflow
from pydantic import BaseModel
from datetime import datetime
from pydantic import BaseModel

# Graph representation of the workflow: view process_invoice.html in your browser

#### Input and Input/Output Type Definitions ####
# Defines the exact data and their types that the invoice agent will focus on extracting
class InvoiceData(BaseModel):
    vendor: str
    amount: float
    description: str
    invoice_date: datetime
    invoice_number: str

# Rule to make sure the input only allows float values
class RuleInput(BaseModel):
    amount: float

# Rule to make sure the output only allows boolean values and a reason string
class RuleOutput(BaseModel):
    approved: bool
    reason: str
#### Input and Output Type Definitions ####

### Mock General Ledger Definition ###
# For this self-contained example, we will use a mock general ledger class
# in a real-world scenario, you would integrate with the API provided 
# by the general ledger system such as Netsuite, QuickBooks, etc.
class JournalEntryLine(BaseModel):
    """Represents a single line in a journal entry (debit or credit)"""
    account_name: str  # e.g., "Office Supplies Expense", "Accounts Payable - ABC"
    debit: float = 0.0
    credit: float = 0.0
    
class JournalEntry(BaseModel):
    """Represents a complete journal entry"""
    entry_date: datetime
    invoice_number: str
    vendor: str
    description: str
    lines: list[JournalEntryLine]
    
    @property
    def is_balanced(self) -> bool:
        """Verify debits equal credits"""
        total_debits = sum(line.debit for line in self.lines)
        total_credits = sum(line.credit for line in self.lines)
        return abs(total_debits - total_credits) < 0.01  # Account for floating point
### Mock General Ledger Definition ####

#### Agent Definition ####
invoice_agent = Agent(
    name="Invoice Agent",
    model="openai:gpt-4.1",
    tools=[],
    max_turns=1,
    system_prompt="Extract vendor, amount, description, invoice date, and invoice number from invoice text.",
    user_prompt="{{input}}",
    input_type=PlanarFile,
    output_type=InvoiceData,
)
#### Agent Definition ####

human_review = Human(
    name="Review Invoice",
    title="Review Invoice",
    input_type=InvoiceData,
    output_type=InvoiceData,
)

# main purpose is to expose the rule in the coplane UI and have business users manually override
# Cannot be used for async functions and interactions with external systems
@rule(description="Auto approve invoices under $1000")
def auto_approve(input: RuleInput) -> RuleOutput:
    return RuleOutput(approved=input.amount < 10, reason="Amount is under $1000")

#### Step Definitions ####
# step 1
@step(display_name="Extract invoice")
async def extract_invoice(invoice_file: PlanarFile) -> InvoiceData:
    result = await invoice_agent(invoice_file)
    return result.output

# step 2
@step(display_name="Maybe approve")
async def maybe_approve(invoice: InvoiceData) -> InvoiceData:
    auto_approve_result = await auto_approve(RuleInput(amount=invoice.amount))
    if auto_approve_result.approved:
        return invoice
    reviewed_invoice = await human_review(invoice, suggested_data=invoice)
    return reviewed_invoice.output

# step 3
# journal entry format:
# Date: November 5, 2025

# Account                          Debit       Credit
# ─────────────────────────────────────────────────────
# Office Supplies Expense          $850.00
#   Accounts Payable - ABC                    $850.00

# Description: Invoice from ABC Office Supplies - Invoice #12345
@step(display_name="Post journal entry to general ledger")
async def write_invoice_to_general_ledger(invoice: InvoiceData) -> JournalEntry:
    """Simulate posting a journal entry for an approved invoice"""
    
    # Create the journal entry with debit and credit lines
    journal_entry = JournalEntry(
        entry_date=invoice.invoice_date,
        invoice_number=invoice.invoice_number,
        vendor=invoice.vendor,
        description=f"Invoice from {invoice.vendor} - Invoice #{invoice.invoice_number}",
        lines=[
            # Debit: Expense (or Asset) - increases expense
            JournalEntryLine(
                account_name="Office Supplies Expense",  # Could be dynamic based on vendor/category
                debit=invoice.amount,
                credit=0.0
            ),
            # Credit: Accounts Payable - increases liability
            JournalEntryLine(
                account_name=f"Accounts Payable - {invoice.vendor}",
                debit=0.0,
                credit=invoice.amount
            )
        ]
    )
    
    # Validate the entry is balanced
    if not journal_entry.is_balanced:
        raise ValueError("Journal entry is not balanced!")
    
    # TODO: In a real system, you'd make an API call here:
    # TODO: mock an API call to the general ledger system
    # await netsuite_client.post_journal_entry(journal_entry)
    
    # For simulation, we log it so it displays in the CoPlane UI
    print(f"\n{'='*60}")
    print(f"JOURNAL ENTRY - {journal_entry.entry_date.strftime('%B %d, %Y')}")
    print(f"{'='*60}")
    print(f"Description: {journal_entry.description}\n")
    print(f"{'Account':<40} {'Debit':>10} {'Credit':>10}")
    print(f"{'-'*60}")
    for line in journal_entry.lines:
        debit_str = f"${line.debit:,.2f}" if line.debit > 0 else ""
        credit_str = f"${line.credit:,.2f}" if line.credit > 0 else ""
        print(f"{line.account_name:<40} {debit_str:>10} {credit_str:>10}")
    print(f"{'='*60}\n")
    
    return journal_entry
#### Step Definitions ####

#### Workflow Definition ####
@workflow()
async def process_invoice(invoice_file: PlanarFile) -> InvoiceData:
    invoice = await extract_invoice(invoice_file)
    invoice_approved = await maybe_approve(invoice)
    if invoice_approved:
        return await write_invoice_to_general_ledger(invoice_approved)
#### Workflow Definition ####

