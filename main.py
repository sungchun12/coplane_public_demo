from planar import PlanarApp

from app.db.entities import Invoice
from app.flows.process_invoice import process_invoice
from app.flows.process_invoice import invoice_agent
from app.flows.complex_invoice_process import complex_invoice_process
from app.flows.complex_invoice_process import invoice_extraction_agent
from app.router import router
from app.flows.process_invoice import auto_approver
from dotenv import load_dotenv

load_dotenv(".env.dev")

app = (
    PlanarApp(title="coplane_public_demo")
    .register_entity(Invoice)
    .register_workflow(process_invoice)
    .register_agent(invoice_agent)
    .register_router(router, prefix="/actions")
)

app.register_rule(auto_approver)
app.register_workflow(complex_invoice_process)
app.register_agent(invoice_extraction_agent)

if __name__ == "__main__":

    print("Planar app is ready!")
    exit(0)