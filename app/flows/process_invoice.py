from planar.ai import Agent
from planar.files import PlanarFile
from planar.human import Human
from planar.rules.decorator import rule
from planar.workflows import step, workflow
from pydantic import BaseModel
from datetime import datetime
from pydantic import BaseModel
import asyncio
from typing import Optional
from xlsxwriter import Workbook
import tempfile
import os

# Graph representation of the workflow: view process_invoice.html in your browser


#### Input and Input/Output Type Definitions ####
# Defines the exact data and their types that the invoice agent will focus on extracting
class InvoiceData(BaseModel):
    file: PlanarFile
    vendor: str
    amount: float
    description: str
    invoice_date: datetime
    invoice_number: str


# Inherits from InvoiceData and adds the approved field
class InvoiceDataReviewed(InvoiceData):
    approved: bool


# Rule to make sure the input only allows amount and threshold float values
class RuleInput(BaseModel):
    amount: float
    threshold: float


# Rule to make sure the output only allows boolean values and a reason string
class RuleOutput(BaseModel):
    approved: bool
    reason: str


#### Input and Output Type Definitions ####


#### Workflow Definition ####
# This is composed of steps marked by `@step`
@workflow()
async def process_invoice(invoice_file: PlanarFile) -> InvoiceData:
    invoice = await extract_invoice(invoice_file)
    invoice_approved = await maybe_approve(invoice)
    if invoice_approved:
        return await write_invoice_to_general_ledger(invoice_approved)


#### Workflow Definition ####


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
    workbook: Optional[PlanarFile] = None

    @property
    def is_balanced(self) -> bool:
        """Verify debits equal credits"""
        total_debits = sum(line.debit for line in self.lines)
        total_credits = sum(line.credit for line in self.lines)
        return abs(total_debits - total_credits) < 0.01  # Account for floating point


### Mock General Ledger Definition ####


### Mock General Ledger API Client ###
class GLApiResponse(BaseModel):
    """Response from the general ledger API"""

    success: bool
    entry_id: str
    message: str
    timestamp: datetime


class MockGeneralLedgerClient:
    """
    Mock client that simulates API calls to a general ledger system.
    In production, this would be replaced with actual API clients like:
    - NetSuite SuiteTalk API
    - QuickBooks Online API
    etc.
    """

    def __init__(
        self,
        base_url: str = "https://api.mockgl.example.com",
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url
        self.api_key = api_key or "mock_api_key_12345"
        self.entry_counter = 1000  # Start with entry ID 1000

    async def post_journal_entry(self, journal_entry: JournalEntry) -> GLApiResponse:
        """
        Simulate posting a journal entry to the general ledger system.
        In a real implementation, this would make an HTTP request to the GL API.
        """
        # Simulate network latency
        await asyncio.sleep(0.5)

        # Validate the entry before "posting"
        # Duplicate validation that is likely built-in to the general ledger system
        if not journal_entry.is_balanced:
            return GLApiResponse(
                success=False,
                entry_id="",
                message="Journal entry is not balanced",
                timestamp=datetime.now(),
            )

        # Simulate successful API response
        self.entry_counter += 1
        entry_id = f"JE-{self.entry_counter}"

        return GLApiResponse(
            success=True,
            entry_id=entry_id,
            message=f"Journal entry posted successfully for vendor {journal_entry.vendor}",
            timestamp=datetime.now(),
        )

    async def get_entry_status(self, entry_id: str) -> dict:
        """Mock method to check status of a posted entry"""
        await asyncio.sleep(0.2)
        return {
            "entry_id": entry_id,
            "status": "posted",
            "posted_date": datetime.now().isoformat(),
        }


### Excel Export Function ###
async def create_journal_entry_excel(journal_entry: JournalEntry) -> PlanarFile:
    """
    Create an Excel file for accountant review of the journal entry.
    Format follows NetSuite journal entry import standards.
    """
    # Create a temporary file
    temp_fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(temp_fd)  # Close the file descriptor immediately

    try:
        # Create workbook
        workbook = Workbook(temp_path)

        # Add formats
        header_format = workbook.add_format(
            {
                "bold": True,
                "bg_color": "#D3D3D3",
                "border": 1,
                "align": "center",
                "valign": "vcenter",
            }
        )

        currency_format = workbook.add_format({"num_format": "$#,##0.00", "border": 1})

        date_format = workbook.add_format({"num_format": "mm/dd/yyyy", "border": 1})

        text_format = workbook.add_format({"border": 1, "align": "left"})

        balanced_format = workbook.add_format(
            {
                "border": 1,
                "bg_color": "#90EE90" if journal_entry.is_balanced else "#FFB6C1",
                "bold": True,
            }
        )

        # Summary Sheet
        summary = workbook.add_worksheet("Summary")
        summary.set_column("A:A", 25)
        summary.set_column("B:B", 20)

        summary.write("A1", "Journal Entry Summary", header_format)
        summary.write("A2", "Invoice Number:", header_format)
        summary.write("B2", journal_entry.invoice_number, text_format)
        summary.write("A3", "Vendor:", header_format)
        summary.write("B3", journal_entry.vendor, text_format)
        summary.write("A4", "Entry Date:", header_format)
        # Remove timezone info for Excel compatibility
        entry_date_naive = (
            journal_entry.entry_date.replace(tzinfo=None)
            if journal_entry.entry_date.tzinfo
            else journal_entry.entry_date
        )
        summary.write("B4", entry_date_naive, date_format)
        summary.write("A5", "Description:", header_format)
        summary.write("B5", journal_entry.description, text_format)

        total_debits = sum(line.debit for line in journal_entry.lines)
        total_credits = sum(line.credit for line in journal_entry.lines)

        summary.write("A7", "Total Debits:", header_format)
        summary.write("B7", total_debits, currency_format)
        summary.write("A8", "Total Credits:", header_format)
        summary.write("B8", total_credits, currency_format)
        summary.write("A9", "Difference:", header_format)
        summary.write("B9", abs(total_debits - total_credits), currency_format)
        summary.write("A10", "Balanced?", header_format)
        summary.write(
            "B10", "Yes" if journal_entry.is_balanced else "No", balanced_format
        )

        # Detail Sheet - NetSuite Format
        detail = workbook.add_worksheet("Journal Entry Details")

        # Set column widths
        detail.set_column("A:A", 12)  # Entry Date
        detail.set_column("B:B", 15)  # Invoice Number
        detail.set_column("C:C", 25)  # Vendor
        detail.set_column("D:D", 40)  # Description
        detail.set_column("E:E", 35)  # Account Name
        detail.set_column("F:F", 12)  # Debit
        detail.set_column("G:G", 12)  # Credit
        detail.set_column("H:H", 30)  # Line Memo
        detail.set_column("I:I", 12)  # Entry Total
        detail.set_column("J:J", 10)  # Balanced?

        # Write headers
        headers = [
            "Entry Date",
            "Invoice Number",
            "Vendor",
            "Description",
            "Account Name",
            "Debit",
            "Credit",
            "Line Memo",
            "Entry Total",
            "Balanced?",
        ]

        for col, header in enumerate(headers):
            detail.write(0, col, header, header_format)

        # Write journal entry lines
        row = 1
        for line in journal_entry.lines:
            detail.write(row, 0, entry_date_naive, date_format)
            detail.write(row, 1, journal_entry.invoice_number, text_format)
            detail.write(row, 2, journal_entry.vendor, text_format)
            detail.write(row, 3, journal_entry.description, text_format)
            detail.write(row, 4, line.account_name, text_format)

            # Write debit/credit - blank if zero
            if line.debit > 0:
                detail.write(row, 5, line.debit, currency_format)
            else:
                detail.write(row, 5, "", text_format)

            if line.credit > 0:
                detail.write(row, 6, line.credit, currency_format)
            else:
                detail.write(row, 6, "", text_format)

            detail.write(row, 7, "", text_format)  # Line memo (empty for now)
            detail.write(row, 8, total_debits, currency_format)
            detail.write(
                row, 9, "Yes" if journal_entry.is_balanced else "No", balanced_format
            )
            row += 1

        # NetSuite Import Format Sheet
        netsuite = workbook.add_worksheet("NetSuite Import Format")
        netsuite.set_column("A:H", 20)

        netsuite.write("A1", "*Journal Entry")
        netsuite.write("A2", "Entry Date")
        netsuite.write("B2", "Subsidiary")
        netsuite.write("C2", "Currency")
        netsuite.write("D2", "Memo")

        netsuite.write("A3", journal_entry.entry_date.strftime("%m/%d/%Y"))
        netsuite.write("B3", "Parent Company")  # Default, can be made dynamic
        netsuite.write("C3", "USD")  # Default, can be made dynamic
        netsuite.write("D3", journal_entry.description)

        netsuite.write("A4", "*Line")
        netsuite.write("A5", "Account")
        netsuite.write("B5", "Debit")
        netsuite.write("C5", "Credit")
        netsuite.write("D5", "Memo")
        netsuite.write("E5", "Department")
        netsuite.write("F5", "Class")
        netsuite.write("G5", "Location")
        netsuite.write("H5", "Entity")

        row = 6
        for line in journal_entry.lines:
            netsuite.write(row, 0, line.account_name)
            if line.debit > 0:
                netsuite.write(row, 1, line.debit, currency_format)
            if line.credit > 0:
                netsuite.write(row, 2, line.credit, currency_format)
            netsuite.write(row, 3, journal_entry.description)
            netsuite.write(row, 7, journal_entry.vendor)
            row += 1

        workbook.close()

        # Read the file and create a PlanarFile
        with open(temp_path, "rb") as f:
            file_content = f.read()

        # Create filename with invoice info
        filename = f"journal_entry_{journal_entry.invoice_number}_{journal_entry.entry_date.strftime('%Y%m%d')}.xlsx"

        # Create PlanarFile
        planar_file = await PlanarFile.upload(
            content=file_content,
            filename=filename,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        return planar_file

    finally:
        # Clean up temp file
        if os.path.exists(temp_path):
            os.remove(temp_path)


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
    output_type=InvoiceDataReviewed,
)


# main purpose is to expose the rule in the coplane UI and have business users manually override
# Cannot be used for async functions and interactions with external systems
@rule(description="Auto approve invoices under $1000 by default")
def auto_approver(input: RuleInput) -> RuleOutput:
    return RuleOutput(
        approved=input.amount < input.threshold,
        reason=f"Amount is under ${input.threshold}",
    )


#### Step Definitions ####
# step 1
@step(display_name="Extract invoice")
async def extract_invoice(invoice_file: PlanarFile) -> InvoiceData:
    result = await invoice_agent(invoice_file)
    return result.output


# step 2
@step(display_name="Maybe approve")
async def maybe_approve(invoice: InvoiceData) -> InvoiceDataReviewed:
    auto_approve_result = await auto_approver(
        RuleInput(amount=invoice.amount, threshold=1000)
    )
    if auto_approve_result.approved:
        return InvoiceDataReviewed(approved=True, **invoice.model_dump())
    else:
        reviewed_invoice = await human_review(invoice, suggested_data=invoice)
        # Access .output to get the InvoiceDataReviewed object from HumanTaskResult
        if reviewed_invoice.output.approved:
            return reviewed_invoice.output
        else:
            raise ValueError("Invoice was not approved by human reviewer")


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
                credit=0.0,
            ),
            # Credit: Accounts Payable - increases liability
            JournalEntryLine(
                account_name=f"Accounts Payable - {invoice.vendor}",
                debit=0.0,
                credit=invoice.amount,
            ),
        ],
    )

    # Validate the entry is balanced
    if not journal_entry.is_balanced:
        raise ValueError("Journal entry is not balanced!")

    # Create Excel file for accountant review
    excel_file = await create_journal_entry_excel(journal_entry)
    journal_entry.workbook = excel_file

    print(f"\n📊 Excel workbook created: {excel_file.filename}")
    print(f"   Ready for accountant review and NetSuite import\n")

    # Initialize the mock client (in production, this would use real credentials)
    gl_client = MockGeneralLedgerClient()
    # Post to the mock general ledger API
    api_response = await gl_client.post_journal_entry(journal_entry)

    if not api_response.success:
        raise ValueError(f"Failed to post journal entry: {api_response.message}")

    # Log the successful posting
    print(f"\n{'*' * 60}")
    print(f"API RESPONSE: {api_response.message}")
    print(f"Entry ID: {api_response.entry_id}")
    print(f"Timestamp: {api_response.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'*' * 60}\n")

    # For simulation, we also log the entry details so it displays in the CoPlane UI
    print(f"\n{'=' * 60}")
    print(f"JOURNAL ENTRY - {journal_entry.entry_date.strftime('%B %d, %Y')}")
    print(f"{'=' * 60}")
    print(f"Description: {journal_entry.description}\n")
    print(f"{'Account':<40} {'Debit':>10} {'Credit':>10}")
    print(f"{'-' * 60}")
    for line in journal_entry.lines:
        debit_str = f"${line.debit:,.2f}" if line.debit > 0 else ""
        credit_str = f"${line.credit:,.2f}" if line.credit > 0 else ""
        print(f"{line.account_name:<40} {debit_str:>10} {credit_str:>10}")
    print(f"{'=' * 60}\n")

    return journal_entry


#### Step Definitions ####
