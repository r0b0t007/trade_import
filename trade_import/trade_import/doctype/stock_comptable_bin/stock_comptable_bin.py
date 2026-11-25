import frappe
from frappe import _
from frappe.model.document import Document


class StockComptableBin(Document):
	"""Running Stock Comptable balance per (Item, Warehouse)."""

	def validate(self):
		if not frappe.flags.in_stock_comptable_posting:
			frappe.throw(_("Le solde Stock Comptable est géré automatiquement."))

	def on_trash(self):
		if not frappe.flags.in_stock_comptable_posting:
			frappe.throw(_("Le solde Stock Comptable ne peut pas être supprimé."))
