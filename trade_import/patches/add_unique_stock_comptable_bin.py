import frappe
from frappe import _
from frappe.query_builder.functions import Count


def execute():
	"""Add the (item, warehouse) unique key to Stock Comptable Bin on sites that already exist.

	New sites get it from the DocType's on_doctype_update. Safe to run twice. If two bins already
	exist for the same pair, it stops with the list of pairs: merge them first, then migrate again.
	"""
	bin_table = frappe.qb.DocType("Stock Comptable Bin")
	duplicates = (
		frappe.qb.from_(bin_table)
		.select(bin_table.item_code, bin_table.warehouse)
		.groupby(bin_table.item_code, bin_table.warehouse)
		.having(Count("*") > 1)
		.run()
	)
	if duplicates:
		frappe.throw(
			_(
				"Soldes de stock comptable en double pour (article, entrepôt) : {0}. Fusionnez-les avant de migrer."
			).format(", ".join(f"({item}, {warehouse})" for item, warehouse in duplicates))
		)

	frappe.db.add_unique(
		"Stock Comptable Bin", ["item_code", "warehouse"], constraint_name="unique_item_warehouse"
	)
