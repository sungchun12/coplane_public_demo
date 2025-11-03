from planar import PlanarApp

from app.db.entities import Invoice
from app.flows.process_invoice import process_invoice
from app.flows.process_invoice import invoice_agent


app = (
    PlanarApp(title="coplane_public_demo")
    .register_entity(Invoice)
    .register_workflow(process_invoice)
    .register_agent(invoice_agent)
)


if __name__ == "__main__":
    print("Planar app is ready!")
    exit(0)