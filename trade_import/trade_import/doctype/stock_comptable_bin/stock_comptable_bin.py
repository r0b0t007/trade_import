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


def on_doctype_update():
	"""One bin per (item, warehouse). Same pattern as ERPNext's own Bin.

	The hash name alone does not stop two simultaneous first postings from each creating a bin.
	"""
	frappe.db.add_unique(
		"Stock Comptable Bin", ["item_code", "warehouse"], constraint_name="unique_item_warehouse"
	)
