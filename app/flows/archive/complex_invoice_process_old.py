"""Full invoice processing workflow - the main entry point.

This workflow orchestrates the complete invoice processing pipeline:
1. Extract invoice data
2. Run intelligent coding agent
3. Verify timesheets (if labor invoice)
4. Verify contract rates (if labor invoice)
5. Route for approval

Each step transitions the invoice through its states.
"""

import uuid
from typing import Optional

from planar.files import PlanarFile
from planar.workflows import workflow
from pydantic import BaseModel, Field

from app.db.entities import (
    InvoiceStatus,
    InvoiceType,
    CodingConfidence,
)
from app.flows.process_invoice import process_invoice_workflow
from app.flows.coding_agent import intelligent_coding_workflow
from app.flows.verify_timesheet import verify_timesheet_workflow
from app.flows.verify_rates import verify_contract_rates_workflow
from app.flows.invoice_state import (
    mark_invoice_pending_approval,
    mark_invoice_approved,
    mark_invoice_exception,
    get_invoice_status,
)


class FullInvoiceResult(BaseModel):
    """Result from full invoice processing pipeline."""

    invoice_id: uuid.UUID
    invoice_number: str
    vendor_name: str
    total_amount: float

    # Final status
    final_status: InvoiceStatus

    # Coding results
    project_code: Optional[str] = None
    coding_confidence: Optional[CodingConfidence] = None
    coding_needs_review: bool = False

    # Verification results
    timesheet_verified: bool = False
    timesheet_variance_pct: float = 0.0
    rates_verified: bool = False
    rate_discrepancies: int = 0

    # Approval routing
    requires_approval: bool = True
    approval_level: Optional[str] = None

    # Issues found
    issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@workflow()
async def full_invoice_workflow(invoice_file: PlanarFile) -> FullInvoiceResult:
    """Process an invoice through the complete pipeline.

    State Transitions:
    - RECEIVED -> EXTRACTED -> PENDING_CODING (process_invoice_workflow)
    - PENDING_CODING -> CODING_IN_PROGRESS -> PENDING_TIMESHEET_VERIFICATION (intelligent_coding_workflow)
    - PENDING_TIMESHEET_VERIFICATION -> PENDING_CONTRACT_VERIFICATION (verify_timesheet_workflow)
    - PENDING_CONTRACT_VERIFICATION -> PENDING_APPROVAL (verify_rates_workflow)
    - PENDING_APPROVAL -> APPROVED/REJECTED (approval step)

    This is the main entry point for invoice processing.
    """
    issues = []
    warnings = []

    # =========================================================================
    # Step 1: Extract and create invoice
    # =========================================================================
    extraction_result = await process_invoice_workflow(invoice_file)

    invoice_id = extraction_result.invoice_id
    invoice_number = extraction_result.invoice_number
    vendor_name = extraction_result.vendor_name
    total_amount = extraction_result.total_amount

    # =========================================================================
    # Step 2: Intelligent coding
    # =========================================================================
    coding_result = await intelligent_coding_workflow(
        invoice_id=invoice_id,
        extracted_data=extraction_result.extracted_data,
        vendor_id=extraction_result.vendor_id,
    )

    project_code = coding_result.coding_decision.project_code
    coding_confidence = coding_result.coding_decision.confidence
    coding_needs_review = coding_result.needs_review

    if coding_result.reviewer_notes:
        warnings.append(coding_result.reviewer_notes)

    if not coding_result.governance_result.approved:
        issues.extend(coding_result.governance_result.blocks)

    # If coding failed or needs review, stop here
    if coding_result.final_status == "blocked":
        return FullInvoiceResult(
            invoice_id=invoice_id,
            invoice_number=invoice_number,
            vendor_name=vendor_name,
            total_amount=total_amount,
            final_status=InvoiceStatus.EXCEPTION,
            project_code=project_code,
            coding_confidence=coding_confidence,
            coding_needs_review=True,
            requires_approval=True,
            issues=issues,
            warnings=warnings,
        )

    # =========================================================================
    # Step 3: Timesheet verification (for labor invoices)
    # =========================================================================
    timesheet_verified = False
    timesheet_variance_pct = 0.0

    has_labor = extraction_result.labor_lines_count > 0
    if has_labor:
        try:
            timesheet_result = await verify_timesheet_workflow(invoice_id)
            timesheet_verified = timesheet_result.all_verified
            timesheet_variance_pct = timesheet_result.total_variance_pct

            if not timesheet_verified:
                warnings.append(
                    f"Timesheet variance: {timesheet_variance_pct:.1f}% "
                    f"({timesheet_result.lines_with_variance} lines with discrepancies)"
                )
        except Exception as e:
            warnings.append(f"Timesheet verification failed: {str(e)}")

    # =========================================================================
    # Step 4: Contract rate verification (for labor invoices)
    # =========================================================================
    rates_verified = False
    rate_discrepancies = 0

    if has_labor:
        try:
            rate_result = await verify_contract_rates_workflow(invoice_id)
            rates_verified = rate_result.all_rates_verified
            rate_discrepancies = rate_result.discrepancy_count

            if not rates_verified:
                warnings.append(
                    f"Rate discrepancies found: {rate_discrepancies} lines, "
                    f"${rate_result.total_overage:.2f} overage"
                )
        except Exception as e:
            warnings.append(f"Rate verification failed: {str(e)}")

    # =========================================================================
    # Step 5: Determine approval routing
    # =========================================================================
    approval_level = None

    # Determine approval level based on amount and confidence
    if total_amount > 100000:
        approval_level = "director"
    elif total_amount > 50000:
        approval_level = "manager"
    elif coding_confidence == CodingConfidence.MEDIUM:
        approval_level = "manager"
    elif coding_confidence == CodingConfidence.LOW:
        approval_level = "director"

    # Auto-approve small, high-confidence invoices with no issues
    auto_approve = (
        total_amount <= 10000 and
        coding_confidence == CodingConfidence.HIGH and
        timesheet_verified and
        rates_verified and
        len(issues) == 0
    )

    if auto_approve:
        await mark_invoice_approved(invoice_id, approver="auto-approved")
        final_status = InvoiceStatus.APPROVED
        requires_approval = False
    else:
        await mark_invoice_pending_approval(invoice_id)
        final_status = InvoiceStatus.PENDING_APPROVAL
        requires_approval = True

    return FullInvoiceResult(
        invoice_id=invoice_id,
        invoice_number=invoice_number,
        vendor_name=vendor_name,
        total_amount=total_amount,
        final_status=final_status,
        project_code=project_code,
        coding_confidence=coding_confidence,
        coding_needs_review=coding_needs_review,
        timesheet_verified=timesheet_verified,
        timesheet_variance_pct=timesheet_variance_pct,
        rates_verified=rates_verified,
        rate_discrepancies=rate_discrepancies,
        requires_approval=requires_approval,
        approval_level=approval_level,
        issues=issues,
        warnings=warnings,
    )
