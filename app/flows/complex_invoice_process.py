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
import re
from pathlib import Path
from planar import get_session
from app.db.entities import ComplexInvoice


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
    """Create Excel file with Dynamics SL Voucher and Adjustment Entry format."""
    temp_fd, temp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(temp_fd)
    
    try:
        workbook = Workbook(temp_path)
        
        # Formats
        header_format = workbook.add_format({
            "bold": True,
            "bg_color": "#D3D3D3",
            "border": 1,
            "align": "left",
        })
        
        label_format = workbook.add_format({
            "bold": True,
            "bg_color": "#E6E6E6",
            "border": 1,
            "align": "left",
        })
        
        currency_format = workbook.add_format({"num_format": "$#,##0.00", "border": 1})
        date_format = workbook.add_format({"num_format": "mm/dd/yyyy", "border": 1})
        text_format = workbook.add_format({"border": 1, "align": "left"})
        number_format = workbook.add_format({"num_format": "0", "border": 1, "align": "right"})
        decimal_format = workbook.add_format({"num_format": "0.00", "border": 1, "align": "right"})
        
        # Main Voucher Entry Sheet
        voucher = workbook.add_worksheet("Voucher Entry")
        voucher.set_column("A:A", 15)
        voucher.set_column("B:B", 25)
        voucher.set_column("C:C", 15)
        voucher.set_column("D:D", 25)
        
        # Batch Section
        row = 0
        voucher.write(row, 0, "BATCH SECTION", header_format)
        voucher.merge_range(f"A{row+1}:D{row+1}", "BATCH SECTION", header_format)
        row += 1
        
        voucher.write(row, 0, "Number:", label_format)
        voucher.write(row, 1, batch.batch_id, text_format)
        voucher.write(row, 2, "Per to Post:", label_format)
        batch_date_naive = (
            batch.batch_date.replace(tzinfo=None)
            if batch.batch_date.tzinfo
            else batch.batch_date
        )
        voucher.write(row, 3, batch_date_naive.strftime("%m-%Y"), text_format)
        row += 1
        
        voucher.write(row, 0, "Entered By:", label_format)
        voucher.write(row, 1, "SYSADMIN", text_format)
        voucher.write(row, 2, "Status:", label_format)
        voucher.write(row, 3, "Posted", text_format)
        row += 1
        
        voucher.write(row, 0, "Handling:", label_format)
        voucher.write(row, 1, "No Action", text_format)
        voucher.write(row, 2, "Total:", label_format)
        voucher.write(row, 3, invoice_data.invoice_amount, currency_format)
        row += 1
        
        voucher.write(row, 0, "Control:", label_format)
        voucher.write(row, 1, invoice_data.invoice_amount, currency_format)
        row += 2
        
        # Document Section
        voucher.write(row, 0, "DOCUMENT SECTION", header_format)
        voucher.merge_range(f"A{row+1}:D{row+1}", "DOCUMENT SECTION", header_format)
        row += 1
        
        # Generate reference number from batch ID
        ref_nbr = batch.batch_id.split("-")[-1] if "-" in batch.batch_id else batch.batch_id[-6:]
        if len(ref_nbr) < 6:
            ref_nbr = ref_nbr.zfill(6)
        
        voucher.write(row, 0, "Ref Nbr:", label_format)
        voucher.write(row, 1, ref_nbr, text_format)
        voucher.write(row, 2, "Type:", label_format)
        voucher.write(row, 3, "Voucher", text_format)
        row += 1
        
        # Generate vendor ID from vendor name (first 8 chars, uppercase)
        vendor_id = invoice_data.vendor[:8].upper().replace(" ", "")
        if len(vendor_id) < 8:
            vendor_id = vendor_id.ljust(8, "0")
        
        invoice_date_naive = (
            invoice_data.invoice_date.replace(tzinfo=None)
            if invoice_data.invoice_date.tzinfo
            else invoice_data.invoice_date
        )
        
        voucher.write(row, 0, "Vendor ID:", label_format)
        voucher.write(row, 1, vendor_id, text_format)
        voucher.write(row, 2, "Vendor Name:", label_format)
        voucher.write(row, 3, invoice_data.vendor, text_format)
        row += 1
        
        voucher.write(row, 0, "Date:", label_format)
        voucher.write(row, 1, invoice_date_naive, date_format)
        voucher.write(row, 2, "Invoice Nbr:", label_format)
        # Extract invoice number from invoice data if available, otherwise generate
        invoice_nbr = getattr(invoice_data, 'invoice_number', None) or f"INV{invoice_date_naive.strftime('%Y%m%d')}"
        voucher.write(row, 3, invoice_nbr, text_format)
        row += 1
        
        voucher.write(row, 0, "Invoice Date:", label_format)
        voucher.write(row, 1, invoice_date_naive, date_format)
        voucher.write(row, 2, "Balance:", label_format)
        voucher.write(row, 3, invoice_data.invoice_amount, currency_format)
        row += 1
        
        voucher.write(row, 0, "Subcontract:", label_format)
        voucher.write(row, 1, "", text_format)
        row += 2
        
        # Voucher/Adjustment Section
        voucher.write(row, 0, "VOUCHER/ADJUSTMENT SECTION", header_format)
        voucher.merge_range(f"A{row+1}:D{row+1}", "VOUCHER/ADJUSTMENT SECTION", header_format)
        row += 1
        
        # Extract terms number (e.g., "Net 45" -> "45")
        terms_number = "45"
        if invoice_data.terms:
            numbers = re.findall(r'\d+', invoice_data.terms)
            if numbers:
                terms_number = numbers[0]
        
        voucher.write(row, 0, "Terms:", label_format)
        voucher.write(row, 1, terms_number, text_format)
        voucher.write(row, 2, invoice_data.terms or f"Net {terms_number}", text_format)
        row += 1
        
        voucher.write(row, 0, "Status:", label_format)
        voucher.write(row, 1, "Active", text_format)
        voucher.write(row, 2, "Amount:", label_format)
        voucher.write(row, 3, invoice_data.invoice_amount, currency_format)
        row += 1
        
        voucher.write(row, 0, "Discount:", label_format)
        discount_amount = invoice_data.discount or 0.0
        voucher.write(row, 1, discount_amount, currency_format)
        voucher.write(row, 2, "Company ID:", label_format)
        # Extract company ID from first line item or default to 10
        company_id = invoice_data.voucher_line_items[0].company if invoice_data.voucher_line_items else "10"
        # If company is a name, convert to ID (mock)
        if not company_id.isdigit():
            company_id = "10"
        voucher.write(row, 3, company_id, text_format)
        row += 1
        
        voucher.write(row, 0, "Company Name:", label_format)
        voucher.write(row, 1, invoice_data.company, text_format)
        voucher.write(row, 2, "PO Nbr:", label_format)
        voucher.write(row, 3, "", text_format)
        row += 1
        
        voucher.write(row, 0, "PO Receipt Nbr:", label_format)
        voucher.write(row, 1, "", text_format)
        voucher.write(row, 2, "Pre-Pay Nbr:", label_format)
        voucher.write(row, 3, "", text_format)
        row += 1
        
        voucher.write(row, 0, "EFT Account:", label_format)
        voucher.write(row, 1, "MAIN", text_format)
        voucher.write(row, 2, "Pay By:", label_format)
        voucher.write(row, 3, "PPD", text_format)
        row += 1
        
        discount_date = invoice_data.discount_date or invoice_date_naive
        due_date = invoice_data.due_date or invoice_date_naive
        pay_date = invoice_data.pay_date or invoice_data.apply_date or due_date
        
        discount_date_naive = (
            discount_date.replace(tzinfo=None)
            if discount_date.tzinfo
            else discount_date
        )
        due_date_naive = (
            due_date.replace(tzinfo=None)
            if due_date.tzinfo
            else due_date
        )
        pay_date_naive = (
            pay_date.replace(tzinfo=None)
            if pay_date.tzinfo
            else pay_date
        )
        
        voucher.write(row, 0, "Discount Date:", label_format)
        voucher.write(row, 1, discount_date_naive, date_format)
        voucher.write(row, 2, "Due Date:", label_format)
        voucher.write(row, 3, due_date_naive, date_format)
        row += 1
        
        voucher.write(row, 0, "Pay Date:", label_format)
        voucher.write(row, 1, pay_date_naive, date_format)
        row += 2
        
        # Detail Grid Section
        voucher.write(row, 0, "DETAIL GRID", header_format)
        voucher.merge_range(f"A{row+1}:L{row+1}", "DETAIL GRID", header_format)
        row += 1
        
        # Detail grid headers
        detail_headers = [
            "Company ID", "Line Type", "Account", "Project", "Task",
            "Employee ID", "Billable", "Subacct", "Invoice Qty",
            "Inv Unit Price", "Inv Ext Price", "Description"
        ]
        
        # Set column widths for detail grid
        voucher.set_column("A:A", 12)  # Company ID
        voucher.set_column("B:B", 12)  # Line Type
        voucher.set_column("C:C", 12)  # Account
        voucher.set_column("D:D", 25)  # Project
        voucher.set_column("E:E", 25)  # Task
        voucher.set_column("F:F", 12)  # Employee ID
        voucher.set_column("G:G", 10)  # Billable
        voucher.set_column("H:H", 15)  # Subacct
        voucher.set_column("I:I", 12)  # Invoice Qty
        voucher.set_column("J:J", 12)  # Inv Unit Price
        voucher.set_column("K:K", 15)  # Inv Ext Price
        voucher.set_column("L:L", 30)  # Description
        
        for col, header in enumerate(detail_headers):
            voucher.write(row, col, header, header_format)
        row += 1
        
        # Write voucher line items
        for line_item in invoice_data.voucher_line_items:
            # Company ID
            company_id_val = line_item.company
            if not company_id_val.isdigit():
                company_id_val = "10"
            voucher.write(row, 0, company_id_val, text_format)
            
            # Line Type
            voucher.write(row, 1, line_item.line_type or "Invoice", text_format)
            
            # Account
            voucher.write(row, 2, line_item.account_number, text_format)
            
            # Project
            voucher.write(row, 3, line_item.project, text_format)
            
            # Task
            voucher.write(row, 4, line_item.task or "", text_format)
            
            # Employee ID
            voucher.write(row, 5, line_item.employee_id, text_format)
            
            # Billable
            voucher.write(row, 6, "Yes" if line_item.billable else "No", text_format)
            
            # Subacct
            voucher.write(row, 7, line_item.subaccount or "00-000-0000", text_format)
            
            # Invoice Qty (mock - calculate from hours if available)
            invoice_qty = 0.00
            voucher.write(row, 8, invoice_qty, decimal_format)
            
            # Inv Unit Price (mock - calculate from ext price and qty)
            unit_price = 0.00
            voucher.write(row, 9, unit_price, decimal_format)
            
            # Inv Ext Price
            voucher.write(row, 10, line_item.invoice_ext_price, currency_format)
            
            # Description
            voucher.write(row, 11, line_item.tran_description, text_format)
            
            row += 1
        
        workbook.close()
        
        # Read and upload as PlanarFile
        with open(temp_path, "rb") as f:
            file_content = f.read()
        
        filename = f"dynamics_sl_voucher_{batch.batch_id}_{batch.batch_date.strftime('%Y%m%d')}.xlsx"
        
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
    """Create batch of voucher entries for Microsoft Dynamics SL.
    
    This creates a voucher entry with line items that will be ingested by Dynamics SL.
    The voucher line items come directly from the invoice data.
    """
    if not approval.approved:
        raise ValueError("Cannot create batch for unapproved invoice")
    
    # Generate batch ID (6-digit number like in the image: 601820)
    # Use a combination of date and vendor to create unique batch number
    batch_date = datetime.now()
    batch_number = int(batch_date.strftime("%y%m%d")) * 10  # Generate 6-digit number
    batch_id = str(batch_number)[:6]
    
    # Create entries list - these represent the voucher line items
    # In Dynamics SL, these are the detail grid entries, not journal entries
    entries = []
    
    for line_item in invoice_data.voucher_line_items:
        entries.append(DynamicsSLTransactionEntry(
            batch_id=batch_id,
            transaction_date=invoice_data.invoice_date,
            account_number=line_item.account_number,
            description=line_item.tran_description,
            debit=0.0,  # Voucher entries don't use debit/credit
            credit=0.0,
            project=line_item.project,
            vendor=invoice_data.vendor,
            invoice_number=getattr(invoice_data, 'invoice_number', None) or f"INV{invoice_data.invoice_date.strftime('%Y%m%d')}",
        ))
    
    batch = DynamicsSLBatch(
        batch_id=batch_id,
        batch_date=batch_date,
        entries=entries,
    )
    
    # Create and attach batch file with voucher entry format
    batch_file = await create_dynamics_sl_batch_file(invoice_data, batch)
    batch.batch_file = batch_file
    
    return batch


# ============================================================================
# Step: Save ComplexInvoice Entity
# ============================================================================


@step(display_name="Save ComplexInvoice Entity")
async def save_complex_invoice_entity(
    invoice_data: ExtractedInvoiceDataReviewed,
) -> ComplexInvoice:
    """Save the complex invoice to the database as an entity."""
    session = get_session()
    
    complex_invoice = ComplexInvoice(
        vendor=invoice_data.vendor,
        company=invoice_data.company,
        invoice_date=invoice_data.invoice_date,
        invoice_amount=invoice_data.invoice_amount,
        terms=invoice_data.terms,
        invoice_currency=invoice_data.invoice_currency,
        voucher_amount=invoice_data.voucher_amount,
        pay_date=invoice_data.pay_date,
        apply_date=invoice_data.apply_date,
        due_date=invoice_data.due_date,
        discount_date=invoice_data.discount_date,
        discount=invoice_data.discount,
        status="reviewed",
        approved=invoice_data.approved,
    )
    
    async with session.begin():
        session.add(complex_invoice)
    
    return complex_invoice


# ============================================================================
# Main Workflow
# ============================================================================


@workflow(is_interactive=True)
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
    
    # Step 3.5: Save to database as entity
    complex_invoice = await save_complex_invoice_entity(reviewed_data)
    
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
        
        # Update entity with batch ID and final status
        session = get_session()
        async with session.begin():
            # Merge the entity to ensure it's tracked in this session
            complex_invoice = await session.merge(complex_invoice)
            complex_invoice.batch_id = dynamics_batch.batch_id
            complex_invoice.status = "completed"
            complex_invoice.approved = True
    
    return WorkflowResult(
        invoice_data=reviewed_data,
        verification_result=verification_result,
        approved=approval.approved,
        dynamics_batch=dynamics_batch,
        status="completed" if approval.approved else "pending_approval",
    )
