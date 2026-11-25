import frappe
from erpnext.stock.utils import get_combine_datetime
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class StockComptableEntry(Document):
	"""Immutable ledger entry tracking accounting stock (Stock Comptable)."""

	def validate(self):
		if not frappe.flags.in_stock_comptable_posting:
			frappe.throw(_("Les écritures de Stock Comptable sont générées automatiquement."))

		if not flt(self.actual_qty, self.precision("actual_qty")):
			frappe.throw(_("La quantité mouvementée ne peut pas être nulle."))

		if not self.posting_datetime and self.posting_date and self.posting_time:
			self.posting_datetime = get_combine_datetime(self.posting_date, self.posting_time)

	def on_trash(self):
		if not frappe.flags.in_stock_comptable_posting:
			frappe.throw(_("Les écritures de Stock Comptable ne peuvent pas être supprimées."))
