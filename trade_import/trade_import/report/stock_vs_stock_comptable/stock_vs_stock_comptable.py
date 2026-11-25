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
	conditions = []
	values = {}

	if filters.get("company"):
		conditions.append("wh.company = %(company)s")
		values["company"] = filters.company
	if filters.get("warehouse"):
		conditions.append("pairs.warehouse = %(warehouse)s")
		values["warehouse"] = filters.warehouse
	if filters.get("item_code"):
		conditions.append("pairs.item_code = %(item_code)s")
		values["item_code"] = filters.item_code

	where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

	rows = frappe.db.sql(
		f"""
		SELECT
			pairs.item_code,
			item.item_name,
			pairs.warehouse,
			item.stock_uom,
			COALESCE(bin.actual_qty, 0) AS stock_qty,
			COALESCE(bin.reserved_qty, 0) AS reserved_qty,
			COALESCE(scb.actual_qty, 0) AS stock_comptable_qty
		FROM (
			SELECT item_code, warehouse FROM `tabBin`
			UNION
			SELECT item_code, warehouse FROM `tabStock Comptable Bin`
		) AS pairs
		INNER JOIN `tabItem` AS item ON item.name = pairs.item_code
		INNER JOIN `tabWarehouse` AS wh ON wh.name = pairs.warehouse
		LEFT JOIN `tabBin` AS bin
			ON bin.item_code = pairs.item_code AND bin.warehouse = pairs.warehouse
		LEFT JOIN `tabStock Comptable Bin` AS scb
			ON scb.item_code = pairs.item_code AND scb.warehouse = pairs.warehouse
		{where_clause}
		ORDER BY pairs.item_code ASC, pairs.warehouse ASC
		""",
		values,
		as_dict=True,
	)

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
