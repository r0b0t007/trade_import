from frappe.model.document import Document

from trade_import import dossier_flow as flow


class DossierImportation(Document):
	"""One shipment: its orders, receipts and costs. The logic is in trade_import.dossier_flow."""

	def validate(self):
		flow.prepare_lines(self)
		flow.set_totals(self)
		self.status = flow.status_of(self)
