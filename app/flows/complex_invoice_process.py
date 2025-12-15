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
from pydantic import BaseModel
from datetime import datetime
from pydantic import BaseModel
import asyncio
from typing import Optional
from xlsxwriter import Workbook
import tempfile
import os
