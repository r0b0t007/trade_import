import frappe
from frappe import _
from frappe.utils import cint, flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = get_data(filters)
	report_summary = get_summary(data)
	return columns, data, None, None, report_summary


def get_columns():
	return [
		{
			"fieldname": "item_code",
			"label": _("Article"),
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"fieldname": "item_name",
			"label": _("Désignation"),
			"fieldtype": "Data",
			"width": 200,
		},
		{
			"fieldname": "warehouse",
			"label": _("Entrepôt"),
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 180,
		},
		{
			"fieldname": "stock_uom",
			"label": _("UDM"),
			"fieldtype": "Link",
			"options": "UOM",
			"width": 80,
		},
		{
			"fieldname": "stock_qty",
			"label": _("Stock réel"),
			"fieldtype": "Float",
			"width": 130,
		},
		{
			"fieldname": "reserved_qty",
			"label": _("Stock réservé"),
			"fieldtype": "Float",
			"width": 130,
		},
		{
			"fieldname": "stock_comptable_qty",
			"label": _("Stock comptable"),
			"fieldtype": "Float",
			"width": 140,
		},
		{
			"fieldname": "difference",
			"label": _("Écart (Comptable - Réel)"),
			"fieldtype": "Float",
			"width": 170,
		},
	]


def get_data(filters):
	precision = cint(frappe.db.get_default("float_precision")) or 3
	item = frappe.qb.DocType("Item")
	warehouse = frappe.qb.DocType("Warehouse")

	def bin_rows(bin_table, *extra_columns):
		"""Rows of one bin table, with the item and warehouse filters of the report applied."""
		query = (
			frappe.qb.from_(bin_table)
			.inner_join(item)
			.on(item.name == bin_table.item_code)
			.inner_join(warehouse)
			.on(warehouse.name == bin_table.warehouse)
			.select(
				bin_table.item_code,
				bin_table.warehouse,
				bin_table.actual_qty,
				item.item_name,
				item.stock_uom,
				*extra_columns,
			)
		)
		if filters.get("company"):
			query = query.where(warehouse.company == filters.company)
		if filters.get("warehouse"):
			query = query.where(bin_table.warehouse == filters.warehouse)
		if filters.get("item_code"):
			query = query.where(bin_table.item_code == filters.item_code)
		return query.run(as_dict=True)

	real_bin = frappe.qb.DocType("Bin")
	comptable_bin = frappe.qb.DocType("Stock Comptable Bin")

	# Every (item, warehouse) pair that exists in either ledger, real or comptable.
	pairs: dict[tuple[str, str], frappe._dict] = {}
	for bin_row in bin_rows(real_bin, real_bin.reserved_qty):
		pairs[(bin_row.item_code, bin_row.warehouse)] = frappe._dict(
			item_code=bin_row.item_code,
			item_name=bin_row.item_name,
			warehouse=bin_row.warehouse,
			stock_uom=bin_row.stock_uom,
			stock_qty=bin_row.actual_qty,
			reserved_qty=bin_row.reserved_qty or 0,
			stock_comptable_qty=0,
		)
	for bin_row in bin_rows(comptable_bin):
		key = (bin_row.item_code, bin_row.warehouse)
		row = pairs.setdefault(
			key,
			frappe._dict(
				item_code=bin_row.item_code,
				item_name=bin_row.item_name,
				warehouse=bin_row.warehouse,
				stock_uom=bin_row.stock_uom,
				stock_qty=0,
				reserved_qty=0,
				stock_comptable_qty=0,
			),
		)
		row.stock_comptable_qty = bin_row.actual_qty or 0

	rows = [pairs[key] for key in sorted(pairs)]

	result = []
	only_diff = cint(filters.get("only_differences"))
	for row in rows:
		stock_qty = flt(row.stock_qty, precision)
		comptable_qty = flt(row.stock_comptable_qty, precision)
		diff = flt(comptable_qty - stock_qty, precision)
		if only_diff and not diff:
			continue
		row.stock_qty = stock_qty
		row.reserved_qty = flt(row.reserved_qty, precision)
		row.stock_comptable_qty = comptable_qty
		row.difference = diff
		result.append(row)

	return result


def get_summary(data):
	precision = cint(frappe.db.get_default("float_precision")) or 3
	total_stock = flt(sum(d.stock_qty for d in data), precision)
	total_comptable = flt(sum(d.stock_comptable_qty for d in data), precision)
	total_diff = flt(total_comptable - total_stock, precision)
	return [
		{
			"value": total_stock,
			"label": _("Stock réel total"),
			"datatype": "Float",
			"indicator": "Blue",
		},
		{
			"value": total_comptable,
			"label": _("Stock comptable total"),
			"datatype": "Float",
			"indicator": "Green",
		},
		{
			"value": total_diff,
			"label": _("Écart total (livré non facturé)"),
			"datatype": "Float",
			"indicator": "Orange" if total_diff else "Grey",
		},
	]
