from planar import PlanarApp

from app.db.entities import Invoice
from app.flows.process_invoice import process_invoice
from app.flows.process_invoice_with_entity import process_invoice_with_entity
from app.flows.process_invoice import invoice_agent
from app.router import router

from dotenv import load_dotenv

load_dotenv(".env.dev")

app = (
    PlanarApp(title="coplane_public_demo")
    .register_entity(Invoice)
    .register_workflow(process_invoice)
    .register_agent(invoice_agent)
    .register_router(router, prefix="/actions")
)

app.register_workflow(process_invoice_with_entity)

if __name__ == "__main__":
    print("Planar app is ready!")
    exit(0)