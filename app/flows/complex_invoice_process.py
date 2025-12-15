"""Full invoice processing workflow - the main entry point.

This workflow orchestrates the complete subcontractor invoice processing pipeline:
1. Step: Download invoice files from FTP and email inboxes (make this events based) [mock]
- it will be a file upload based on the local file system
2. Agent: Extract invoice data from the files
- this step will require creative liberties to mock the lookup data (ex: match invoice company name to a company in the UnaNet API)
- need to create an agent that can extract the invoice data from the file and validate the data
- the agent should be able to extract the following data:
  - Vendor (ex: ABC Consulting)
  - Company (ex: GCOM)
  - invoice date
  - invoice amount
  - terms
  - pay date
  - apply date
  - due date
  - discount date
  - discount
  - invoice currency
  - voucher line items (table that contains the following columns: company, line type, account number, project, task, employee id, billable, account description, subaccount, invoice ext price, tran description, external reference number)
  - voucher amount
3. Step: Human review step to review the invoice data was extracted correctly and other lookup mapping data is correct  (manual review)
5. Step: Export data from UnaNet API in memory [mock]
6. Step: Verify the data extracted from the invoice file matches the data exported from UnaNet API(timesheets, contract rates) (output is an excel file with a pivot table to display the validations) [mock]
- includes a PlanarFile output that can be downloaded by the user
7. Step: Route to AP team for Approval 
8. Step: CoPlane creates a batch of transaction entries staged in Microsoft Dynamics SL [mock]


Each step transitions the invoice through its states.

Design Notes:
- Do not need to use planar rules for now
- Everything should be in one file for now
"""


from planar.ai import Agent
from planar.files import PlanarFile
from planar.human import Human
from planar.rules.decorator import rule
from planar.workflows import step, workflow
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List
import asyncio
from xlsxwriter import Workbook
import tempfile
import os
from pathlib import Path


# ============================================================================
# Data Models
# ============================================================================


class VoucherLineItem(BaseModel):
    """Represents a single line item in the invoice voucher."""
    company: str
    line_type: str
    account_number: str
    project: str
    task: Optional[str] = None
    employee_id: str
    billable: bool
    account_description: str
    subaccount: Optional[str] = None
    invoice_ext_price: float
    tran_description: str
    external_reference_number: Optional[str] = None


class ExtractedInvoiceData(BaseModel):
    """Extracted invoice data from the invoice file."""
    vendor: str
    company: str
    invoice_date: datetime
    invoice_amount: float
    terms: str
    pay_date: Optional[datetime] = None
    apply_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    discount_date: Optional[datetime] = None
    discount: Optional[float] = None
    invoice_currency: str = "USD"
    voucher_line_items: List[VoucherLineItem] = Field(default_factory=list)
    voucher_amount: float


class ExtractedInvoiceDataReviewed(ExtractedInvoiceData):
    """Extracted invoice data after human review."""
    approved: bool


class UnaNetTimesheetEntry(BaseModel):
    """Represents a timesheet entry from UnaNet."""
    employee_id: str
    employee_name: str
    project_code: str
    task: Optional[str] = None
    work_period: str
    hours: float
    billable: bool


class UnaNetContractRate(BaseModel):
    """Represents a contract rate from UnaNet."""
    employee_id: str
    employee_name: str
    project_code: str
    role: str
    hourly_rate: float
    effective_date: datetime


class UnaNetExportData(BaseModel):
    """Mock data exported from UnaNet API."""
    timesheets: List[UnaNetTimesheetEntry] = Field(default_factory=list)
    contract_rates: List[UnaNetContractRate] = Field(default_factory=list)
    export_date: datetime


class VerificationResult(BaseModel):
    """Result from verifying invoice data against UnaNet data."""
    invoice_data: ExtractedInvoiceData
    unanet_data: UnaNetExportData
    timesheet_matches: int = 0
    timesheet_discrepancies: int = 0
    rate_matches: int = 0
    rate_discrepancies: int = 0
    verification_file: PlanarFile
    summary: str = ""


class APApprovalDecision(BaseModel):
    """AP team approval decision."""
    approved: bool
    notes: Optional[str] = None
    approver: Optional[str] = None


class DynamicsSLTransactionEntry(BaseModel):
    """Represents a transaction entry for Microsoft Dynamics SL."""
    batch_id: str
    transaction_date: datetime
    account_number: str
    description: str
    debit: float = 0.0
    credit: float = 0.0
    project: Optional[str] = None
    vendor: str
    invoice_number: str


class DynamicsSLBatch(BaseModel):
    """Batch of transaction entries for Microsoft Dynamics SL."""
    batch_id: str
    batch_date: datetime
    entries: List[DynamicsSLTransactionEntry] = Field(default_factory=list)
    batch_file: Optional[PlanarFile] = None


class WorkflowResult(BaseModel):
    """Final result from the complete workflow."""
    invoice_data: ExtractedInvoiceData
    verification_result: VerificationResult
    approved: bool
    dynamics_batch: Optional[DynamicsSLBatch] = None
    status: str = "completed"


# ============================================================================
# Step 1: File Upload (Mock)
# ============================================================================


@step(display_name="Upload Invoice File")
async def upload_invoice_file() -> PlanarFile:
    """Mock file upload step - automatically loads the invoice file."""
    invoice_path = Path(__file__).parent.parent.parent / "private" / "subcontractor_invoice.png"
    
    if not invoice_path.exists():
        raise FileNotFoundError(f"Invoice file not found at {invoice_path}")
    
    # Read the file content
    with open(invoice_path, "rb") as f:
        file_content = f.read()
    
    # Upload as PlanarFile
    planar_file = await PlanarFile.upload(
        content=file_content,
        filename="subcontractor_invoice.png",
        content_type="image/png",
    )
    
    return planar_file


# ============================================================================
# Step 2: Extract Invoice Agent
# ============================================================================


invoice_extraction_agent = Agent(
    name="Invoice Extraction Agent",
    model="openai:gpt-4.1",
    tools=[],
    max_turns=1,
    system_prompt="""You are an expert invoice data extraction agent. Extract all invoice data from the provided invoice file.

Extract the following fields:
- Vendor name (e.g., ABC Consulting)
- Company name (e.g., GCOM)
- Invoice date
- Invoice amount (total)
- Payment terms (e.g., Net 45)
- Pay date (if specified)
- Apply date (if specified)
- Due date (calculated from invoice date and terms)
- Discount date (if specified)
- Discount amount (if specified)
- Invoice currency (default to USD if not specified)
- Voucher line items: Extract all line items from the invoice table. Each line item should include:
  - Company
  - Line type (e.g., Labor, Expense)
  - Account number (derive from project/account mapping)
  - Project code/name
  - Task (if specified)
  - Employee ID (extract from employee name or use name as ID)
  - Billable (true for labor invoices)
  - Account description
  - Subaccount (if specified)
  - Invoice extended price (amount for this line)
  - Transaction description
  - External reference number (if specified)
- Voucher amount (sum of all line items)

Use creative liberties to mock lookup data:
- Match company name to appropriate UnaNet company codes
- Derive account numbers from project codes
- Map employee names to employee IDs

Return the data in the ExtractedInvoiceData format.""",
    user_prompt="{{input}}",
    input_type=PlanarFile,
    output_type=ExtractedInvoiceData,
)


@step(display_name="Extract Invoice Data")
async def extract_invoice_data(invoice_file: PlanarFile) -> ExtractedInvoiceData:
    """Extract invoice data using the AI agent."""
    result = await invoice_extraction_agent(invoice_file)
    return result.output


# ============================================================================
# Step 3: Human Review
# ============================================================================


human_review = Human(
    name="Review Invoice Data",
    title="Review Invoice Data",
    input_type=ExtractedInvoiceData,
    output_type=ExtractedInvoiceDataReviewed,
)


@step(display_name="Review Invoice Data")
async def review_invoice_data(invoice_data: ExtractedInvoiceData) -> ExtractedInvoiceDataReviewed:
    """Human review step for invoice data."""
    reviewed = await human_review(invoice_data, suggested_data=invoice_data)
    return reviewed.output


# ============================================================================
# Step 4: Export UnaNet API Data (Mock)
# ============================================================================


@step(display_name="Export UnaNet API Data")
async def export_unanet_data(invoice_data: ExtractedInvoiceDataReviewed) -> UnaNetExportData:
    """Mock function that simulates exporting data from UnaNet API."""
    # Simulate API latency
    await asyncio.sleep(0.5)
    
    # Mock timesheet data based on invoice line items
    timesheets = []
    for line_item in invoice_data.voucher_line_items:
        # Extract work period from invoice date (assume monthly)
        work_period = invoice_data.invoice_date.strftime("%B %Y")
        
        timesheets.append(UnaNetTimesheetEntry(
            employee_id=line_item.employee_id,
            employee_name=line_item.tran_description.split(" - ")[0] if " - " in line_item.tran_description else line_item.employee_id,
            project_code=line_item.project,
            task=line_item.task,
            work_period=work_period,
            hours=line_item.invoice_ext_price / 75.0 if line_item.invoice_ext_price > 0 else 0.0,  # Mock hours calculation
            billable=line_item.billable,
        ))
    
    # Mock contract rates based on invoice line items
    contract_rates = []
    for line_item in invoice_data.voucher_line_items:
        # Derive rate from invoice amount and hours
        hours = line_item.invoice_ext_price / 75.0 if line_item.invoice_ext_price > 0 else 0.0
        rate = line_item.invoice_ext_price / hours if hours > 0 else 75.0
        
        contract_rates.append(UnaNetContractRate(
            employee_id=line_item.employee_id,
            employee_name=line_item.tran_description.split(" - ")[0] if " - " in line_item.tran_description else line_item.employee_id,
            project_code=line_item.project,
            role=line_item.account_description,
            hourly_rate=rate,
            effective_date=invoice_data.invoice_date,
        ))
    
    return UnaNetExportData(
        timesheets=timesheets,
        contract_rates=contract_rates,
        export_date=datetime.now(),
    )


# ============================================================================
# Step 5: Verify Data Match
# ============================================================================


async def create_verification_excel(
    invoice_data: ExtractedInvoiceData,
    unanet_data: UnaNetExportData,
) -> PlanarFile:
    """Create Excel file with verification results and pivot-like summary tables."""
    temp_fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(temp_fd)
    
    try:
        workbook = Workbook(temp_path)
        
        # Formats
        header_format = workbook.add_format({
            "bold": True,
            "bg_color": "#D3D3D3",
            "border": 1,
            "align": "center",
            "valign": "vcenter",
        })
        
        currency_format = workbook.add_format({"num_format": "$#,##0.00", "border": 1})
        date_format = workbook.add_format({"num_format": "mm/dd/yyyy", "border": 1})
        text_format = workbook.add_format({"border": 1, "align": "left"})
        match_format = workbook.add_format({"bg_color": "#90EE90", "border": 1})
        mismatch_format = workbook.add_format({"bg_color": "#FFB6C1", "border": 1})
        
        # Summary Sheet
        summary = workbook.add_worksheet("Summary")
        summary.set_column("A:A", 30)
        summary.set_column("B:B", 25)
        
        summary.write("A1", "Invoice Verification Summary", header_format)
        summary.merge_range("A1:B1", "Invoice Verification Summary", header_format)
        
        row = 2
        summary.write(row, 0, "Vendor:", header_format)
        summary.write(row, 1, invoice_data.vendor, text_format)
        row += 1
        
        summary.write(row, 0, "Company:", header_format)
        summary.write(row, 1, invoice_data.company, text_format)
        row += 1
        
        summary.write(row, 0, "Invoice Date:", header_format)
        invoice_date_naive = (
            invoice_data.invoice_date.replace(tzinfo=None)
            if invoice_data.invoice_date.tzinfo
            else invoice_data.invoice_date
        )
        summary.write(row, 1, invoice_date_naive, date_format)
        row += 1
        
        summary.write(row, 0, "Invoice Amount:", header_format)
        summary.write(row, 1, invoice_data.invoice_amount, currency_format)
        row += 1
        
        summary.write(row, 0, "UnaNet Export Date:", header_format)
        export_date_naive = (
            unanet_data.export_date.replace(tzinfo=None)
            if unanet_data.export_date.tzinfo
            else unanet_data.export_date
        )
        summary.write(row, 1, export_date_naive, date_format)
        row += 2
        
        # Verification Results
        summary.write(row, 0, "Verification Results", header_format)
        summary.merge_range(f"A{row+1}:B{row+1}", "Verification Results", header_format)
        row += 1
        
        # Count matches and discrepancies
        timesheet_matches = 0
        timesheet_discrepancies = 0
        rate_matches = 0
        rate_discrepancies = 0
        
        # Compare timesheets
        invoice_employee_hours = {}
        for line_item in invoice_data.voucher_line_items:
            key = (line_item.employee_id, line_item.project)
            if key not in invoice_employee_hours:
                invoice_employee_hours[key] = 0.0
            invoice_employee_hours[key] += line_item.invoice_ext_price / 75.0  # Mock hours
        
        for timesheet in unanet_data.timesheets:
            key = (timesheet.employee_id, timesheet.project_code)
            if key in invoice_employee_hours:
                invoice_hours = invoice_employee_hours[key]
                if abs(invoice_hours - timesheet.hours) < 0.1:
                    timesheet_matches += 1
                else:
                    timesheet_discrepancies += 1
            else:
                timesheet_discrepancies += 1
        
        # Compare rates
        invoice_employee_rates = {}
        for line_item in invoice_data.voucher_line_items:
            hours = line_item.invoice_ext_price / 75.0 if line_item.invoice_ext_price > 0 else 0.0
            rate = line_item.invoice_ext_price / hours if hours > 0 else 75.0
            invoice_employee_rates[line_item.employee_id] = rate
        
        for rate_entry in unanet_data.contract_rates:
            if rate_entry.employee_id in invoice_employee_rates:
                invoice_rate = invoice_employee_rates[rate_entry.employee_id]
                if abs(invoice_rate - rate_entry.hourly_rate) < 1.0:
                    rate_matches += 1
                else:
                    rate_discrepancies += 1
            else:
                rate_discrepancies += 1
        
        summary.write(row, 0, "Timesheet Matches:", header_format)
        summary.write(row, 1, timesheet_matches, text_format)
        row += 1
        
        summary.write(row, 0, "Timesheet Discrepancies:", header_format)
        summary.write(row, 1, timesheet_discrepancies, mismatch_format if timesheet_discrepancies > 0 else text_format)
        row += 1
        
        summary.write(row, 0, "Rate Matches:", header_format)
        summary.write(row, 1, rate_matches, text_format)
        row += 1
        
        summary.write(row, 0, "Rate Discrepancies:", header_format)
        summary.write(row, 1, rate_discrepancies, mismatch_format if rate_discrepancies > 0 else text_format)
        
        # Pivot-like Summary Table
        pivot = workbook.add_worksheet("Pivot Summary")
        pivot.set_column("A:A", 20)
        pivot.set_column("B:B", 15)
        pivot.set_column("C:C", 15)
        pivot.set_column("D:D", 15)
        pivot.set_column("E:E", 15)
        
        headers = ["Employee", "Project", "Invoice Hours", "UnaNet Hours", "Match"]
        for col, header in enumerate(headers):
            pivot.write(0, col, header, header_format)
        
        row = 1
        for line_item in invoice_data.voucher_line_items:
            invoice_hours = line_item.invoice_ext_price / 75.0  # Mock calculation
            unanet_hours = 0.0
            match = False
            
            for timesheet in unanet_data.timesheets:
                if (timesheet.employee_id == line_item.employee_id and 
                    timesheet.project_code == line_item.project):
                    unanet_hours = timesheet.hours
                    match = abs(invoice_hours - unanet_hours) < 0.1
                    break
            
            pivot.write(row, 0, line_item.employee_id, text_format)
            pivot.write(row, 1, line_item.project, text_format)
            pivot.write(row, 2, invoice_hours, text_format)
            pivot.write(row, 3, unanet_hours, text_format)
            pivot.write(row, 4, "Yes" if match else "No", match_format if match else mismatch_format)
            row += 1
        
        # Timesheet Comparison Detail
        timesheet_detail = workbook.add_worksheet("Timesheet Comparison")
        timesheet_detail.set_column("A:A", 20)
        timesheet_detail.set_column("B:B", 15)
        timesheet_detail.set_column("C:C", 15)
        timesheet_detail.set_column("D:D", 15)
        timesheet_detail.set_column("E:E", 15)
        timesheet_detail.set_column("F:F", 15)
        
        headers = ["Employee", "Project", "Invoice Hours", "UnaNet Hours", "Difference", "Status"]
        for col, header in enumerate(headers):
            timesheet_detail.write(0, col, header, header_format)
        
        row = 1
        for line_item in invoice_data.voucher_line_items:
            invoice_hours = line_item.invoice_ext_price / 75.0
            unanet_hours = 0.0
            
            for timesheet in unanet_data.timesheets:
                if (timesheet.employee_id == line_item.employee_id and 
                    timesheet.project_code == line_item.project):
                    unanet_hours = timesheet.hours
                    break
            
            difference = invoice_hours - unanet_hours
            status = "Match" if abs(difference) < 0.1 else "Mismatch"
            
            timesheet_detail.write(row, 0, line_item.employee_id, text_format)
            timesheet_detail.write(row, 1, line_item.project, text_format)
            timesheet_detail.write(row, 2, invoice_hours, text_format)
            timesheet_detail.write(row, 3, unanet_hours, text_format)
            timesheet_detail.write(row, 4, difference, text_format)
            timesheet_detail.write(row, 5, status, match_format if status == "Match" else mismatch_format)
            row += 1
        
        # Rate Comparison Detail
        rate_detail = workbook.add_worksheet("Rate Comparison")
        rate_detail.set_column("A:A", 20)
        rate_detail.set_column("B:B", 15)
        rate_detail.set_column("C:C", 15)
        rate_detail.set_column("D:D", 15)
        rate_detail.set_column("E:E", 15)
        rate_detail.set_column("F:F", 15)
        
        headers = ["Employee", "Project", "Invoice Rate", "UnaNet Rate", "Difference", "Status"]
        for col, header in enumerate(headers):
            rate_detail.write(0, col, header, header_format)
        
        row = 1
        for line_item in invoice_data.voucher_line_items:
            hours = line_item.invoice_ext_price / 75.0 if line_item.invoice_ext_price > 0 else 0.0
            invoice_rate = line_item.invoice_ext_price / hours if hours > 0 else 75.0
            unanet_rate = 0.0
            
            for rate_entry in unanet_data.contract_rates:
                if (rate_entry.employee_id == line_item.employee_id and 
                    rate_entry.project_code == line_item.project):
                    unanet_rate = rate_entry.hourly_rate
                    break
            
            difference = invoice_rate - unanet_rate
            status = "Match" if abs(difference) < 1.0 else "Mismatch"
            
            rate_detail.write(row, 0, line_item.employee_id, text_format)
            rate_detail.write(row, 1, line_item.project, text_format)
            rate_detail.write(row, 2, invoice_rate, currency_format)
            rate_detail.write(row, 3, unanet_rate, currency_format)
            rate_detail.write(row, 4, difference, currency_format)
            rate_detail.write(row, 5, status, match_format if status == "Match" else mismatch_format)
            row += 1
        
        workbook.close()
        
        # Read and upload as PlanarFile
        with open(temp_path, "rb") as f:
            file_content = f.read()
        
        filename = f"verification_{invoice_data.vendor}_{invoice_data.invoice_date.strftime('%Y%m%d')}.xlsx"
        
        planar_file = await PlanarFile.upload(
            content=file_content,
            filename=filename,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        
        return planar_file, timesheet_matches, timesheet_discrepancies, rate_matches, rate_discrepancies
    
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@step(display_name="Verify Data Match")
async def verify_data_match(
    invoice_data: ExtractedInvoiceData,
    unanet_data: UnaNetExportData,
) -> VerificationResult:
    """Verify invoice data matches UnaNet data and generate Excel report."""
    verification_file, timesheet_matches, timesheet_discrepancies, rate_matches, rate_discrepancies = await create_verification_excel(
        invoice_data, unanet_data
    )
    
    summary = f"Verified {len(invoice_data.voucher_line_items)} line items. "
    summary += f"Timesheets: {timesheet_matches} matches, {timesheet_discrepancies} discrepancies. "
    summary += f"Rates: {rate_matches} matches, {rate_discrepancies} discrepancies."
    
    return VerificationResult(
        invoice_data=invoice_data,
        unanet_data=unanet_data,
        timesheet_matches=timesheet_matches,
        timesheet_discrepancies=timesheet_discrepancies,
        rate_matches=rate_matches,
        rate_discrepancies=rate_discrepancies,
        verification_file=verification_file,
        summary=summary,
    )


# ============================================================================
# Step 6: Route to AP Team
# ============================================================================


ap_approval = Human(
    name="AP Team Approval",
    title="AP Team Approval",
    input_type=VerificationResult,
    output_type=APApprovalDecision,
)


@step(display_name="Route to AP Team")
async def route_to_ap_team(verification_result: VerificationResult) -> APApprovalDecision:
    """Route verification results to AP team for approval."""
    approval = await ap_approval(verification_result, suggested_data=APApprovalDecision(
        approved=verification_result.timesheet_discrepancies == 0 and verification_result.rate_discrepancies == 0,
        notes=verification_result.summary,
    ))
    return approval.output


# ============================================================================
# Step 7: Create Dynamics SL Batch (Mock)
# ============================================================================


async def create_dynamics_sl_batch_file(
    invoice_data: ExtractedInvoiceData,
    batch: DynamicsSLBatch,
) -> PlanarFile:
    """Create Excel file with Dynamics SL batch entries."""
    temp_fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(temp_fd)
    
    try:
        workbook = Workbook(temp_path)
        
        # Formats
        header_format = workbook.add_format({
            "bold": True,
            "bg_color": "#D3D3D3",
            "border": 1,
            "align": "center",
        })
        
        currency_format = workbook.add_format({"num_format": "$#,##0.00", "border": 1})
        date_format = workbook.add_format({"num_format": "mm/dd/yyyy", "border": 1})
        text_format = workbook.add_format({"border": 1, "align": "left"})
        
        # Dynamics SL Import Format Sheet
        dynamics = workbook.add_worksheet("Dynamics SL Import")
        dynamics.set_column("A:H", 20)
        
        # Header
        dynamics.write("A1", "*Batch Header", header_format)
        dynamics.write("A2", "Batch ID", header_format)
        dynamics.write("B2", "Batch Date", header_format)
        dynamics.write("C2", "Description", header_format)
        
        batch_date_naive = (
            batch.batch_date.replace(tzinfo=None)
            if batch.batch_date.tzinfo
            else batch.batch_date
        )
        dynamics.write("A3", batch.batch_id, text_format)
        dynamics.write("B3", batch_date_naive, date_format)
        dynamics.write("C3", f"Invoice Batch - {invoice_data.vendor}", text_format)
        
        # Transaction Lines
        dynamics.write("A5", "*Transaction Lines", header_format)
        headers = ["Transaction Date", "Account", "Description", "Debit", "Credit", "Project", "Vendor", "Invoice #"]
        for col, header in enumerate(headers):
            dynamics.write(5, col, header, header_format)
        
        row = 6
        for entry in batch.entries:
            entry_date_naive = (
                entry.transaction_date.replace(tzinfo=None)
                if entry.transaction_date.tzinfo
                else entry.transaction_date
            )
            dynamics.write(row, 0, entry_date_naive, date_format)
            dynamics.write(row, 1, entry.account_number, text_format)
            dynamics.write(row, 2, entry.description, text_format)
            if entry.debit > 0:
                dynamics.write(row, 3, entry.debit, currency_format)
            if entry.credit > 0:
                dynamics.write(row, 4, entry.credit, currency_format)
            if entry.project:
                dynamics.write(row, 5, entry.project, text_format)
            dynamics.write(row, 6, entry.vendor, text_format)
            dynamics.write(row, 7, entry.invoice_number, text_format)
            row += 1
        
        workbook.close()
        
        # Read and upload as PlanarFile
        with open(temp_path, "rb") as f:
            file_content = f.read()
        
        filename = f"dynamics_sl_batch_{batch.batch_id}_{batch.batch_date.strftime('%Y%m%d')}.xlsx"
        
        planar_file = await PlanarFile.upload(
            content=file_content,
            filename=filename,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        
        return planar_file
    
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@step(display_name="Create Dynamics SL Batch")
async def create_dynamics_sl_batch(
    invoice_data: ExtractedInvoiceData,
    approval: APApprovalDecision,
) -> DynamicsSLBatch:
    """Create batch of transaction entries for Microsoft Dynamics SL."""
    if not approval.approved:
        raise ValueError("Cannot create batch for unapproved invoice")
    
    # Generate batch ID
    batch_id = f"BATCH-{invoice_data.invoice_date.strftime('%Y%m%d')}-{invoice_data.vendor[:3].upper()}"
    batch_date = datetime.now()
    
    entries = []
    
    # Create expense entry (debit)
    entries.append(DynamicsSLTransactionEntry(
        batch_id=batch_id,
        transaction_date=invoice_data.invoice_date,
        account_number="6100-100",  # Mock account number
        description=f"Invoice from {invoice_data.vendor}",
        debit=invoice_data.invoice_amount,
        credit=0.0,
        project=invoice_data.voucher_line_items[0].project if invoice_data.voucher_line_items else None,
        vendor=invoice_data.vendor,
        invoice_number=f"INV-{invoice_data.invoice_date.strftime('%Y%m%d')}",
    ))
    
    # Create accounts payable entry (credit)
    entries.append(DynamicsSLTransactionEntry(
        batch_id=batch_id,
        transaction_date=invoice_data.invoice_date,
        account_number="2000-100",  # Mock AP account
        description=f"Accounts Payable - {invoice_data.vendor}",
        debit=0.0,
        credit=invoice_data.invoice_amount,
        vendor=invoice_data.vendor,
        invoice_number=f"INV-{invoice_data.invoice_date.strftime('%Y%m%d')}",
    ))
    
    batch = DynamicsSLBatch(
        batch_id=batch_id,
        batch_date=batch_date,
        entries=entries,
    )
    
    # Create and attach batch file
    batch_file = await create_dynamics_sl_batch_file(invoice_data, batch)
    batch.batch_file = batch_file
    
    return batch


# ============================================================================
# Main Workflow
# ============================================================================


@workflow()
async def complex_invoice_process() -> WorkflowResult:
    """Main workflow orchestrating the complete invoice processing pipeline."""
    # Step 1: Upload invoice file
    invoice_file = await upload_invoice_file()
    
    # Step 2: Extract invoice data
    invoice_data = await extract_invoice_data(invoice_file)
    
    # Step 3: Human review
    reviewed_data = await review_invoice_data(invoice_data)
    
    if not reviewed_data.approved:
        raise ValueError("Invoice data was not approved during review")
    
    # Step 4: Export UnaNet data
    unanet_data = await export_unanet_data(reviewed_data)
    
    # Step 5: Verify data match
    verification_result = await verify_data_match(reviewed_data, unanet_data)
    
    # Step 6: Route to AP team
    approval = await route_to_ap_team(verification_result)
    
    # Step 7: Create Dynamics SL batch (only if approved)
    dynamics_batch = None
    if approval.approved:
        dynamics_batch = await create_dynamics_sl_batch(reviewed_data, approval)
    
    return WorkflowResult(
        invoice_data=reviewed_data,
        verification_result=verification_result,
        approved=approval.approved,
        dynamics_batch=dynamics_batch,
        status="completed" if approval.approved else "pending_approval",
    )
