"""Intelligent Invoice Coding Agent with Tools and Governance.

This agent has wide latitude to search projects, vendors, initiatives, budgets,
and GL accounts to correctly attribute invoices. It uses bound tools for searching
and matching, with governance rules to ensure proper thresholds and approvals.
"""

import json
import uuid
from datetime import date
from typing import List, Optional

from planar import get_session
from planar.ai import Agent
from planar.rules.decorator import rule
from planar.workflows import step, workflow
from pydantic import BaseModel, Field
from sqlmodel import select

from app.flows.invoice_state import (
    mark_invoice_coding_in_progress,
    mark_invoice_pending_timesheet_verification,
    mark_invoice_exception,
)
from app.db.entities import (
    Budget,
    BudgetAllocation,
    CodingConfidence,
    CodingDecision,
    Contract,
    Department,
    ExpenseCategory,
    ExtractedInvoiceData,
    Fund,
    GLAccount,
    GLAccountType,
    Initiative,
    Invoice,
    InvoiceStatus,
    InvoiceType,
    LaborCategory,
    Project,
    ProjectMatch,
    ProjectStatus,
    ProjectVendorAssignment,
    TaskOrder,
    Vendor,
)


# ============================================================================
# Tool Response Models
# ============================================================================


class VendorSearchResult(BaseModel):
    """Result from vendor search."""
    vendor_id: uuid.UUID
    vendor_code: str
    name: str
    aliases: Optional[str]
    default_gl_account: Optional[str]


class ProjectSearchResult(BaseModel):
    """Result from project search."""
    project_id: uuid.UUID
    code: str
    name: str
    status: str
    department_name: str
    initiative_name: Optional[str]
    labor_budget: float
    labor_spent: float
    expense_budget: float
    expense_spent: float
    keywords: Optional[str]
    vendor_aliases: Optional[str]
    unanet_project_code: Optional[str]


class VendorProjectAssignment(BaseModel):
    """Vendor assignment to a project."""
    project_id: uuid.UUID
    project_code: str
    project_name: str
    role: Optional[str]
    budget_amount: float
    spent_amount: float
    remaining: float


class GLAccountSearchResult(BaseModel):
    """Result from GL account search."""
    gl_account_id: uuid.UUID
    account_number: str
    name: str
    account_type: str
    expense_categories: Optional[str]
    labor_categories: Optional[str]
    keywords: Optional[str]


class BudgetSearchResult(BaseModel):
    """Result from budget search."""
    budget_allocation_id: uuid.UUID
    project_code: str
    fund_code: str
    gl_account_number: Optional[str]
    allocated_amount: float
    spent_amount: float
    remaining_amount: float
    utilization_percentage: float


class InitiativeSearchResult(BaseModel):
    """Result from initiative search."""
    initiative_id: uuid.UUID
    code: str
    name: str
    status: str
    total_budget: float
    spent_to_date: float
    keywords: Optional[str]
    project_count: int


# ============================================================================
# Coding Agent Tools (Steps that the agent can call)
# ============================================================================


@step(display_name="Search Vendors")
async def search_vendors(
    name_query: Optional[str] = None,
    vendor_code: Optional[str] = None,
) -> List[VendorSearchResult]:
    """Search for vendors by name or code.

    The agent should use this to identify which vendor submitted the invoice.
    Supports fuzzy matching on name and aliases.
    """
    session = get_session()

    stmt = select(Vendor).where(Vendor.is_active == True)

    if vendor_code:
        stmt = stmt.where(Vendor.vendor_code.ilike(f"%{vendor_code}%"))
    elif name_query:
        # Search name and aliases
        stmt = stmt.where(
            (Vendor.name.ilike(f"%{name_query}%")) |
            (Vendor.name_aliases.ilike(f"%{name_query}%"))
        )

    result = await session.exec(stmt)
    vendors = result.all()

    return [
        VendorSearchResult(
            vendor_id=v.id,
            vendor_code=v.vendor_code,
            name=v.name,
            aliases=v.name_aliases,
            default_gl_account=None,  # Would join to get this
        )
        for v in vendors[:10]  # Limit results
    ]


@step(display_name="Get Vendor Project Assignments")
async def get_vendor_projects(vendor_id: uuid.UUID) -> List[VendorProjectAssignment]:
    """Get all projects a vendor is assigned to.

    This is critical for matching invoices to the right project.
    Shows what projects this vendor is approved to bill against.
    """
    session = get_session()

    stmt = select(ProjectVendorAssignment, Project).join(
        Project, ProjectVendorAssignment.project_id == Project.id
    ).where(
        ProjectVendorAssignment.vendor_id == vendor_id,
        ProjectVendorAssignment.is_active == True,
        Project.is_active == True,
    )

    result = await session.exec(stmt)
    assignments = result.all()

    return [
        VendorProjectAssignment(
            project_id=proj.id,
            project_code=proj.code,
            project_name=proj.name,
            role=assign.role,
            budget_amount=assign.budget_amount,
            spent_amount=assign.spent_amount,
            remaining=assign.budget_amount - assign.spent_amount,
        )
        for assign, proj in assignments
    ]


@step(display_name="Search Projects")
async def search_projects(
    query: Optional[str] = None,
    department_code: Optional[str] = None,
    initiative_code: Optional[str] = None,
    unanet_code: Optional[str] = None,
    status: Optional[str] = None,
) -> List[ProjectSearchResult]:
    """Search for projects by various criteria.

    The agent uses this to find potential project matches based on:
    - Keywords in invoice or contract
    - Department assignments
    - Initiative groupings
    - Unanet project codes
    """
    session = get_session()

    stmt = select(Project, Department).join(
        Department, Project.department_id == Department.id
    ).where(Project.is_active == True)

    if query:
        stmt = stmt.where(
            (Project.name.ilike(f"%{query}%")) |
            (Project.code.ilike(f"%{query}%")) |
            (Project.keywords.ilike(f"%{query}%")) |
            (Project.vendor_aliases.ilike(f"%{query}%"))
        )

    if unanet_code:
        stmt = stmt.where(Project.unanet_project_code.ilike(f"%{unanet_code}%"))

    if status:
        stmt = stmt.where(Project.status == ProjectStatus(status))
    else:
        stmt = stmt.where(Project.status == ProjectStatus.ACTIVE)

    result = await session.exec(stmt)
    projects = result.all()

    results = []
    for proj, dept in projects[:15]:  # Limit results
        # Get initiative name if linked
        initiative_name = None
        if proj.initiative_id:
            initiative = await session.get(Initiative, proj.initiative_id)
            if initiative:
                initiative_name = initiative.name

        results.append(ProjectSearchResult(
            project_id=proj.id,
            code=proj.code,
            name=proj.name,
            status=proj.status.value,
            department_name=dept.name,
            initiative_name=initiative_name,
            labor_budget=proj.labor_budget,
            labor_spent=proj.labor_spent,
            expense_budget=proj.expense_budget,
            expense_spent=proj.expense_spent,
            keywords=proj.keywords,
            vendor_aliases=proj.vendor_aliases,
            unanet_project_code=proj.unanet_project_code,
        ))

    return results


@step(display_name="Search GL Accounts")
async def search_gl_accounts(
    query: Optional[str] = None,
    account_type: Optional[str] = None,
    expense_category: Optional[str] = None,
    labor_category: Optional[str] = None,
) -> List[GLAccountSearchResult]:
    """Search for GL accounts for expense classification.

    The agent uses this to find the right GL account based on:
    - Invoice line types (labor vs expense)
    - Expense categories (travel, equipment, etc.)
    - Labor categories (consulting, technical, etc.)
    - Keywords from invoice descriptions
    """
    session = get_session()

    stmt = select(GLAccount).where(GLAccount.is_active == True)

    if query:
        stmt = stmt.where(
            (GLAccount.name.ilike(f"%{query}%")) |
            (GLAccount.account_number.ilike(f"%{query}%")) |
            (GLAccount.keywords.ilike(f"%{query}%"))
        )

    if account_type:
        stmt = stmt.where(GLAccount.account_type == GLAccountType(account_type))

    if expense_category:
        stmt = stmt.where(GLAccount.expense_categories.ilike(f"%{expense_category}%"))

    if labor_category:
        stmt = stmt.where(GLAccount.labor_categories.ilike(f"%{labor_category}%"))

    result = await session.exec(stmt)
    accounts = result.all()

    return [
        GLAccountSearchResult(
            gl_account_id=a.id,
            account_number=a.account_number,
            name=a.name,
            account_type=a.account_type.value,
            expense_categories=a.expense_categories,
            labor_categories=a.labor_categories,
            keywords=a.keywords,
        )
        for a in accounts[:10]
    ]


@step(display_name="Search Initiatives")
async def search_initiatives(
    query: Optional[str] = None,
) -> List[InitiativeSearchResult]:
    """Search for strategic initiatives.

    Initiatives group related projects and may have their own budgets.
    """
    session = get_session()

    stmt = select(Initiative).where(Initiative.is_active == True)

    if query:
        stmt = stmt.where(
            (Initiative.name.ilike(f"%{query}%")) |
            (Initiative.code.ilike(f"%{query}%")) |
            (Initiative.keywords.ilike(f"%{query}%"))
        )

    result = await session.exec(stmt)
    initiatives = result.all()

    results = []
    for init in initiatives[:10]:
        # Count projects in this initiative
        proj_stmt = select(Project).where(
            Project.initiative_id == init.id,
            Project.is_active == True,
        )
        proj_result = await session.exec(proj_stmt)
        project_count = len(proj_result.all())

        results.append(InitiativeSearchResult(
            initiative_id=init.id,
            code=init.code,
            name=init.name,
            status=init.status.value,
            total_budget=init.total_budget,
            spent_to_date=init.spent_to_date,
            keywords=init.keywords,
            project_count=project_count,
        ))

    return results


@step(display_name="Get Project Budget Allocations")
async def get_project_budgets(project_id: uuid.UUID) -> List[BudgetSearchResult]:
    """Get all budget allocations for a project.

    Shows available funding and utilization. Critical for:
    - Ensuring budget exists for the charge
    - Warning when budget is running low
    - Selecting the right fund source
    """
    session = get_session()

    stmt = select(BudgetAllocation, Fund).join(
        Fund, BudgetAllocation.fund_id == Fund.id
    ).where(
        BudgetAllocation.project_id == project_id,
        BudgetAllocation.is_active == True,
    )

    result = await session.exec(stmt)
    allocations = result.all()

    results = []
    for alloc, fund in allocations:
        # Get project code
        project = await session.get(Project, alloc.project_id)
        project_code = project.code if project else "UNKNOWN"

        # Get GL account number if set
        gl_account_number = None
        if alloc.gl_account_id:
            gl_account = await session.get(GLAccount, alloc.gl_account_id)
            if gl_account:
                gl_account_number = gl_account.account_number

        utilization = (alloc.spent_amount / alloc.allocated_amount * 100) if alloc.allocated_amount > 0 else 0

        results.append(BudgetSearchResult(
            budget_allocation_id=alloc.id,
            project_code=project_code,
            fund_code=fund.code,
            gl_account_number=gl_account_number,
            allocated_amount=alloc.allocated_amount,
            spent_amount=alloc.spent_amount,
            remaining_amount=alloc.remaining_amount,
            utilization_percentage=round(utilization, 1),
        ))

    return results


@step(display_name="Get Contract Details")
async def get_contract_details(
    contract_number: Optional[str] = None,
    vendor_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    """Get contract details including linked task orders.

    Used to validate invoice is against valid contract and
    may help identify the project through task order linkage.
    """
    session = get_session()

    stmt = select(Contract).where(Contract.is_active == True)

    if contract_number:
        stmt = stmt.where(Contract.contract_number.ilike(f"%{contract_number}%"))
    elif vendor_id:
        stmt = stmt.where(Contract.vendor_id == vendor_id)
    else:
        return None

    result = await session.exec(stmt)
    contract = result.first()

    if not contract:
        return None

    # Get task orders
    to_stmt = select(TaskOrder).where(
        TaskOrder.contract_id == contract.id,
        TaskOrder.is_active == True,
    )
    to_result = await session.exec(to_stmt)
    task_orders = to_result.all()

    return {
        "contract_id": str(contract.id),
        "contract_number": contract.contract_number,
        "title": contract.title,
        "vendor_id": str(contract.vendor_id),
        "contract_type": contract.contract_type.value,
        "total_ceiling": contract.total_ceiling,
        "invoiced_amount": contract.invoiced_amount,
        "remaining": contract.total_ceiling - contract.invoiced_amount,
        "task_orders": [
            {
                "task_order_number": to.task_order_number,
                "project_id": str(to.project_id) if to.project_id else None,
                "title": to.title,
                "ceiling_amount": to.ceiling_amount,
                "invoiced_amount": to.invoiced_amount,
            }
            for to in task_orders
        ],
    }


# ============================================================================
# Governance Rules
# ============================================================================


class CodingGovernanceInput(BaseModel):
    """Input for coding governance check."""
    invoice_amount: float
    invoice_type: InvoiceType
    coding_decision: CodingDecision
    project_budget_remaining: float
    budget_utilization_percentage: float
    vendor_budget_remaining: float


class CodingGovernanceResult(BaseModel):
    """Result from governance check."""
    approved: bool
    confidence_override: Optional[CodingConfidence] = None
    warnings: List[str] = Field(default_factory=list)
    blocks: List[str] = Field(default_factory=list)
    requires_approval_from: Optional[str] = None
    approval_threshold_reason: Optional[str] = None


@rule(description="Check coding decision against governance thresholds")
def coding_governance_rule(input: CodingGovernanceInput) -> CodingGovernanceResult:
    """Apply governance rules to coding decisions.

    Thresholds:
    - High confidence (>90%): Auto-approve if within budget
    - Medium confidence (70-90%): Flag for review
    - Low confidence (<70%): Requires manual coding

    Budget checks:
    - >90% utilization: Warning
    - Invoice would exceed budget: Block
    - No budget allocation: Block

    Amount thresholds:
    - >$100k: Requires director approval regardless
    - >$50k: Requires manager approval if medium confidence
    """
    result = CodingGovernanceResult(approved=True)

    # Check budget constraints
    if input.project_budget_remaining <= 0:
        result.approved = False
        result.blocks.append("Project budget is exhausted")

    if input.invoice_amount > input.project_budget_remaining:
        result.approved = False
        result.blocks.append(
            f"Invoice amount (${input.invoice_amount:,.2f}) exceeds remaining budget "
            f"(${input.project_budget_remaining:,.2f})"
        )

    if input.budget_utilization_percentage > 90:
        result.warnings.append(
            f"Budget utilization is at {input.budget_utilization_percentage:.1f}%"
        )

    if input.budget_utilization_percentage > 95:
        result.warnings.append("CRITICAL: Budget nearly exhausted")

    # Check vendor budget
    if input.vendor_budget_remaining < input.invoice_amount:
        result.warnings.append(
            f"Invoice exceeds vendor's remaining project allocation "
            f"(${input.vendor_budget_remaining:,.2f})"
        )

    # Confidence-based rules
    decision = input.coding_decision

    if decision.confidence == CodingConfidence.LOW:
        result.approved = False
        result.confidence_override = CodingConfidence.LOW
        result.blocks.append("Low confidence coding requires manual review")

    if decision.confidence == CodingConfidence.MEDIUM:
        result.warnings.append("Medium confidence - recommend review")
        if input.invoice_amount > 50000:
            result.requires_approval_from = "manager"
            result.approval_threshold_reason = "Medium confidence + amount > $50k"

    # Amount-based approval requirements
    if input.invoice_amount > 100000:
        result.requires_approval_from = "director"
        result.approval_threshold_reason = "Invoice amount exceeds $100k"
    elif input.invoice_amount > 50000 and not result.requires_approval_from:
        result.requires_approval_from = "manager"
        result.approval_threshold_reason = "Invoice amount exceeds $50k"

    return result


class GLAccountMappingInput(BaseModel):
    """Input for GL account mapping rule."""
    invoice_type: InvoiceType
    labor_categories: List[LaborCategory] = Field(default_factory=list)
    expense_categories: List[ExpenseCategory] = Field(default_factory=list)


class GLAccountMappingOutput(BaseModel):
    """Output from GL account mapping rule."""
    gl_account_number: str
    gl_account_description: str


@rule(description="Determine GL account based on invoice type and categories")
def gl_account_mapping_rule(input: GLAccountMappingInput) -> GLAccountMappingOutput:
    """Rule-based GL account determination as fallback.

    Used when agent can't find a match or as validation.
    """
    # Labor-based invoices
    if input.invoice_type == InvoiceType.TIME_AND_MATERIALS:
        # Check labor categories
        technical_cats = {
            LaborCategory.DEVELOPER,
            LaborCategory.TECHNICAL_LEAD,
            LaborCategory.SOLUTION_ARCHITECT,
            LaborCategory.INTEGRATION_SPECIALIST,
        }
        if any(cat in technical_cats for cat in input.labor_categories):
            return GLAccountMappingOutput(
                gl_account_number="6100-200",
                gl_account_description="Technical consulting services"
            )
        return GLAccountMappingOutput(
            gl_account_number="6100-100",
            gl_account_description="Professional services"
        )

    # Expense-based invoices
    if input.invoice_type == InvoiceType.EXPENSE_REIMBURSEMENT:
        travel_cats = {
            ExpenseCategory.TRAVEL_AIRFARE,
            ExpenseCategory.TRAVEL_LODGING,
            ExpenseCategory.TRAVEL_MEALS,
            ExpenseCategory.TRAVEL_GROUND,
            ExpenseCategory.TRAVEL_OTHER,
        }
        if any(cat in travel_cats for cat in input.expense_categories):
            return GLAccountMappingOutput(
                gl_account_number="6200-100",
                gl_account_description="Travel - domestic"
            )
        if ExpenseCategory.SOFTWARE_LICENSE in input.expense_categories:
            return GLAccountMappingOutput(
                gl_account_number="6300-100",
                gl_account_description="Software licenses"
            )
        if ExpenseCategory.TRAINING_MATERIALS in input.expense_categories:
            return GLAccountMappingOutput(
                gl_account_number="6400-100",
                gl_account_description="Training and development"
            )
        return GLAccountMappingOutput(
            gl_account_number="6900-100",
            gl_account_description="Other direct expenses"
        )

    # Mixed or milestone
    return GLAccountMappingOutput(
        gl_account_number="6100-100",
        gl_account_description="Professional services"
    )


# ============================================================================
# The Intelligent Coding Agent
# ============================================================================


class InvoiceCodingInput(BaseModel):
    """Input for the coding agent."""
    extracted_data: ExtractedInvoiceData
    vendor_id: Optional[uuid.UUID] = None
    contract_id: Optional[uuid.UUID] = None
    hints: Optional[dict] = Field(default=None, description="Additional hints for coding")


invoice_coding_agent = Agent(
    name="intelligent_invoice_coder",
    model="openai:gpt-4.1",
    max_turns=10,  # Give it room to explore
    tools=[
        search_vendors,
        get_vendor_projects,
        search_projects,
        search_gl_accounts,
        search_initiatives,
        get_project_budgets,
        get_contract_details,
    ],
    system_prompt="""You are an expert accounts payable analyst specializing in government
ERP implementation projects. Your job is to correctly code vendor invoices to the
right PROJECT, INITIATIVE, DEPARTMENT, GL ACCOUNT, and FUND.

## Your Mission
Invoices MUST be attributed to the correct project. This is critical because:
1. Projects have budgets that track spending
2. Departments need accurate cost reporting
3. Initiatives track strategic program spending
4. GL accounts ensure proper expense classification
5. Funds ensure money comes from the right source

## Your Approach
1. IDENTIFY THE VENDOR - Use search_vendors to find and confirm the vendor
2. FIND VENDOR'S PROJECTS - Use get_vendor_projects to see what projects this vendor is authorized to bill
3. MATCH TO PROJECT - Use the invoice details (contract #, task order, employee names, descriptions) to identify the specific project
4. VERIFY BUDGET - Use get_project_budgets to ensure budget exists and isn't exhausted
5. SELECT GL ACCOUNT - Use search_gl_accounts to find the right expense classification
6. CHECK CONTRACT - Use get_contract_details if there's a contract reference

## Key Matching Strategies
- Contract numbers often directly link to projects via task orders
- Vendor employee names may be listed in project vendor assignments
- Keywords in invoice descriptions may match project keywords
- Unanet project codes should match if present

## Confidence Levels
- HIGH (>90%): Strong match - vendor assigned to exactly one project, or clear contract/task order link
- MEDIUM (70-90%): Likely match - vendor on multiple projects but one is clearly more relevant
- LOW (<70%): Uncertain - multiple plausible projects or no clear match

## Important Rules
- NEVER guess - if unsure, return LOW confidence
- ALWAYS check budget availability
- Flag any budget warnings (>80% utilized)
- Consider alternative projects and explain why they were rejected

You have wide latitude to explore. Use the tools to gather information before making a decision.""",
    user_prompt="""Code this invoice to the correct project and accounts:

## Invoice Data
**Vendor:** {{ input.extracted_data.header.vendor_name }}
**Invoice #:** {{ input.extracted_data.header.invoice_number }}
**Date:** {{ input.extracted_data.header.invoice_date }}
**Period:** {{ input.extracted_data.header.period_start }} to {{ input.extracted_data.header.period_end }}
**Contract #:** {{ input.extracted_data.header.contract_number }}
**Task Order:** {{ input.extracted_data.header.task_order_number }}
**Project Reference:** {{ input.extracted_data.header.project_reference }}
**Total Amount:** ${{ input.extracted_data.header.total_amount }}
**Labor Total:** ${{ input.extracted_data.header.labor_total }}
**Expense Total:** ${{ input.extracted_data.header.expense_total }}

## Labor Lines
{% for line in input.extracted_data.labor_lines %}
- {{ line.employee_name }} | {{ line.labor_category }} | {{ line.hours }}hrs @ ${{ line.hourly_rate }} = ${{ line.amount }}
{% endfor %}

## Expense Lines
{% for line in input.extracted_data.expense_lines %}
- {{ line.category }} | {{ line.description }} | ${{ line.amount }}
{% endfor %}

{% if input.vendor_id %}
**Known Vendor ID:** {{ input.vendor_id }}
{% endif %}

{% if input.hints %}
**Hints:** {{ input.hints }}
{% endif %}

Search for the vendor, find their project assignments, and determine the correct coding.
Return a complete CodingDecision with your reasoning.""",
    input_type=InvoiceCodingInput,
    output_type=CodingDecision,
)


# ============================================================================
# Coding Workflow
# ============================================================================


class CodingWorkflowResult(BaseModel):
    """Result from the coding workflow."""
    invoice_id: uuid.UUID
    coding_decision: CodingDecision
    governance_result: CodingGovernanceResult
    final_status: str
    needs_review: bool
    reviewer_notes: Optional[str]


@workflow()
async def intelligent_coding_workflow(
    invoice_id: uuid.UUID,
    extracted_data: ExtractedInvoiceData,
    vendor_id: Optional[uuid.UUID] = None,
) -> CodingWorkflowResult:
    """Run the intelligent coding agent and apply governance.

    Steps:
    1. Prepare input for the coding agent
    2. Run the agent with its tools
    3. Apply governance rules
    4. Update the invoice with coding decision
    5. Route for approval if needed
    """
    session = get_session()

    # Get the invoice
    invoice = await session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError(f"Invoice {invoice_id} not found")

    # Transition to CODING_IN_PROGRESS state
    await mark_invoice_coding_in_progress(invoice_id)

    # Prepare agent input
    agent_input = InvoiceCodingInput(
        extracted_data=extracted_data,
        vendor_id=vendor_id,
    )

    # Run the coding agent
    agent_result = await invoice_coding_agent(agent_input)
    coding_decision = agent_result.output

    # Get budget information for governance
    project_budget_remaining = 0.0
    budget_utilization = 0.0
    vendor_budget_remaining = 0.0

    if coding_decision.project_id:
        budgets = await get_project_budgets(coding_decision.project_id)
        if budgets:
            total_remaining = sum(b.remaining_amount for b in budgets)
            total_allocated = sum(b.allocated_amount for b in budgets)
            project_budget_remaining = total_remaining
            budget_utilization = ((total_allocated - total_remaining) / total_allocated * 100) if total_allocated > 0 else 0

        # Get vendor budget
        if vendor_id:
            vendor_projects = await get_vendor_projects(vendor_id)
            for vp in vendor_projects:
                if vp.project_id == coding_decision.project_id:
                    vendor_budget_remaining = vp.remaining
                    break

    # Apply governance rules
    gov_input = CodingGovernanceInput(
        invoice_amount=invoice.total_amount,
        invoice_type=invoice.invoice_type,
        coding_decision=coding_decision,
        project_budget_remaining=project_budget_remaining,
        budget_utilization_percentage=budget_utilization,
        vendor_budget_remaining=vendor_budget_remaining,
    )

    governance_result = await coding_governance_rule(gov_input)

    # Determine final status
    needs_review = False
    final_status = "coded"

    if not governance_result.approved:
        final_status = "blocked"
        needs_review = True
    elif governance_result.confidence_override == CodingConfidence.LOW:
        final_status = "low_confidence"
        needs_review = True
    elif governance_result.requires_approval_from:
        final_status = f"pending_{governance_result.requires_approval_from}_approval"
        needs_review = True
    elif coding_decision.confidence == CodingConfidence.MEDIUM:
        final_status = "pending_review"
        needs_review = True

    # Update invoice with coding
    invoice.project_id = coding_decision.project_id
    invoice.initiative_id = coding_decision.initiative_id
    invoice.department_id = coding_decision.department_id
    invoice.gl_account_id = coding_decision.gl_account_id
    invoice.fund_id = coding_decision.fund_id
    invoice.budget_allocation_id = coding_decision.budget_allocation_id

    invoice.project_code = coding_decision.project_code
    invoice.gl_account = coding_decision.gl_account_code
    invoice.cost_center = coding_decision.cost_center
    invoice.fund_code = coding_decision.fund_code

    invoice.coding_confidence = coding_decision.confidence
    invoice.coding_reasoning = coding_decision.reasoning
    invoice.coding_alternatives = json.dumps([
        {
            "project_id": str(p.project_id),
            "project_code": p.project_code,
            "confidence": p.confidence,
            "reasons": p.match_reasons,
        }
        for p in coding_decision.alternative_projects
    ])

    session.add(invoice)
    await session.commit()

    # Build reviewer notes
    reviewer_notes = None
    if governance_result.warnings or governance_result.blocks:
        notes = []
        if governance_result.blocks:
            notes.append("BLOCKS: " + "; ".join(governance_result.blocks))
        if governance_result.warnings:
            notes.append("WARNINGS: " + "; ".join(governance_result.warnings))
        if governance_result.approval_threshold_reason:
            notes.append(f"APPROVAL REQUIRED: {governance_result.approval_threshold_reason}")
        reviewer_notes = "\n".join(notes)

    # Transition to appropriate state based on governance result
    if governance_result.approved and not needs_review:
        await mark_invoice_pending_timesheet_verification(invoice_id)
    else:
        await mark_invoice_exception(
            invoice_id,
            reason=reviewer_notes or "Coding requires review"
        )

    return CodingWorkflowResult(
        invoice_id=invoice_id,
        coding_decision=coding_decision,
        governance_result=governance_result,
        final_status=final_status,
        needs_review=needs_review,
        reviewer_notes=reviewer_notes,
    )
