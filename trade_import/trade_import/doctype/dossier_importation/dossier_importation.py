from frappe.model.document import Document


class DossierImportation(Document):
	"""One shipment: its orders, receipts and costs. The logic is in trade_import.dossier_flow."""
