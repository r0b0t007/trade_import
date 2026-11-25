import frappe
from erpnext.stock.utils import get_combine_datetime
from frappe import _
from frappe.utils import cint, flt, nowdate, nowtime

SETTINGS_DOCTYPE = "Trade Import Settings"
ENTRY_DOCTYPE = "Stock Comptable Entry"
BIN_DOCTYPE = "Stock Comptable Bin"


def _get_settings():
	return frappe.get_cached_doc(SETTINGS_DOCTYPE)


def is_stock_comptable_enabled() -> bool:
	"""Return True when the parallel Stock Comptable ledger is enabled."""
	return bool(cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "enable_stock_comptable")))


def is_delivery_note_required() -> bool:
	"""Return True when a submitted Delivery Note (BL) is mandatory before invoicing."""
	return bool(cint(frappe.db.get_single_value(SETTINGS_DOCTYPE, "require_delivery_note_before_invoice")))


def get_stock_comptable_qty(item_code: str, warehouse: str) -> float:
	"""Return the current Stock Comptable balance for one item and warehouse."""
	qty = frappe.db.get_value(
		BIN_DOCTYPE,
		{"item_code": item_code, "warehouse": warehouse},
		"actual_qty",
	)
	return flt(qty, _qty_precision())


def _qty_precision() -> int:
	return (
		cint(frappe.get_precision(ENTRY_DOCTYPE, "actual_qty"))
		or cint(frappe.db.get_default("float_precision"))
		or 3
	)


def _is_stock_item(item_code: str | None) -> bool:
	if not item_code:
		return False
	return bool(cint(frappe.get_cached_value("Item", item_code, "is_stock_item")))


def _allow_ignore_linked_entries(doc) -> None:
	ignored = tuple(doc.get("ignore_linked_doctypes") or ())
	if ENTRY_DOCTYPE not in ignored:
		doc.ignore_linked_doctypes = (*ignored, ENTRY_DOCTYPE)


def _get_or_create_bin(
	item_code: str, warehouse: str, company: str, stock_uom: str | None
) -> tuple[str, float]:
	"""Lock and return (bin_name, current_qty) for (item_code, warehouse)."""
	precision = _qty_precision()
	existing = frappe.db.get_value(
		BIN_DOCTYPE,
		{"item_code": item_code, "warehouse": warehouse},
		["name", "actual_qty"],
		as_dict=True,
		for_update=True,
	)
	if existing:
		return existing.name, flt(existing.actual_qty, precision)

	bin_doc = frappe.get_doc(
		{
			"doctype": BIN_DOCTYPE,
			"item_code": item_code,
			"warehouse": warehouse,
			"company": company,
			"stock_uom": stock_uom or frappe.get_cached_value("Item", item_code, "stock_uom"),
			"actual_qty": 0.0,
		}
	)
	bin_doc.insert(ignore_permissions=True)
	return bin_doc.name, 0.0


def _post_movements(movements: list[dict], voucher) -> None:
	"""Apply signed Stock Comptable movements atomically for a submitted voucher."""
	if not movements:
		return

	precision = _qty_precision()
	allow_negative = bool(cint(_get_settings().allow_negative_stock_comptable))
	posting_date = voucher.get("posting_date") or nowdate()
	posting_time = voucher.get("posting_time") or nowtime()
	posting_datetime = get_combine_datetime(posting_date, posting_time)

	prev_flag = frappe.flags.in_stock_comptable_posting
	frappe.flags.in_stock_comptable_posting = True
	try:
		for move in movements:
			qty = flt(move["actual_qty"], precision)
			if not qty:
				continue

			item_code = move["item_code"]
			warehouse = move["warehouse"]
			stock_uom = move.get("stock_uom") or frappe.get_cached_value("Item", item_code, "stock_uom")

			bin_name, current_qty = _get_or_create_bin(item_code, warehouse, voucher.company, stock_uom)
			new_qty = flt(current_qty + qty, precision)

			if new_qty < 0 and not allow_negative:
				frappe.throw(
					_(
						"Stock comptable insuffisant pour l'article {0} dans l'entrepôt {1} : "
						"disponible {2}, mouvement {3}."
					).format(item_code, warehouse, current_qty, qty)
				)

			frappe.db.set_value(BIN_DOCTYPE, bin_name, "actual_qty", new_qty, update_modified=True)
			frappe.get_doc(
				{
					"doctype": ENTRY_DOCTYPE,
					"item_code": item_code,
					"warehouse": warehouse,
					"company": voucher.company,
					"stock_uom": stock_uom,
					"posting_date": posting_date,
					"posting_time": posting_time,
					"posting_datetime": posting_datetime,
					"voucher_type": voucher.doctype,
					"voucher_no": voucher.name,
					"voucher_detail_no": move.get("voucher_detail_no"),
					"delivery_note": move.get("delivery_note"),
					"actual_qty": qty,
					"qty_after_transaction": new_qty,
					"is_cancelled": 0,
				}
			).insert(ignore_permissions=True)
	finally:
		frappe.flags.in_stock_comptable_posting = prev_flag


def _reverse_voucher_entries(voucher) -> None:
	"""Mark voucher entries cancelled and reverse their Stock Comptable Bin impact."""
	_allow_ignore_linked_entries(voucher)
	entries = frappe.get_all(
		ENTRY_DOCTYPE,
		filters={
			"voucher_type": voucher.doctype,
			"voucher_no": voucher.name,
			"is_cancelled": 0,
		},
		fields=["name", "item_code", "warehouse", "actual_qty", "stock_uom"],
		order_by="creation asc",
	)
	if not entries:
		return

	precision = _qty_precision()
	allow_negative = bool(cint(_get_settings().allow_negative_stock_comptable))

	prev_flag = frappe.flags.in_stock_comptable_posting
	frappe.flags.in_stock_comptable_posting = True
	try:
		for entry in entries:
			bin_name, current_qty = _get_or_create_bin(
				entry.item_code, entry.warehouse, voucher.company, entry.stock_uom
			)
			reversed_qty = flt(current_qty - flt(entry.actual_qty, precision), precision)
			if reversed_qty < 0 and not allow_negative:
				frappe.throw(
					_(
						"Impossible d'annuler {0} : le stock comptable de {1} dans {2} deviendrait négatif ({3})."
					).format(voucher.name, entry.item_code, entry.warehouse, reversed_qty)
				)

			frappe.db.set_value(BIN_DOCTYPE, bin_name, "actual_qty", reversed_qty, update_modified=True)
			frappe.db.set_value(ENTRY_DOCTYPE, entry.name, "is_cancelled", 1, update_modified=True)
	finally:
		frappe.flags.in_stock_comptable_posting = prev_flag


def validate_sales_invoice(doc, method=None) -> None:
	"""Enforce BL (Delivery Note) before Sales Invoice when configured (D-05)."""
	if not is_delivery_note_required():
		return
	if cint(doc.is_return) or cint(doc.is_debit_note):
		return

	if cint(doc.update_stock):
		frappe.throw(
			_(
				"La mise à jour directe du stock sur facture est interdite lorsque le bon de livraison est obligatoire."
			)
		)

	for row in doc.get("items") or []:
		if not _is_stock_item(row.item_code):
			continue
		if not row.delivery_note:
			frappe.throw(
				_(
					"Ligne {0} : un bon de livraison validé est obligatoire pour facturer l'article {1}."
				).format(row.idx, row.item_code)
			)
		dn_status = frappe.db.get_value("Delivery Note", row.delivery_note, "docstatus")
		if cint(dn_status) != 1:
			frappe.throw(
				_("Ligne {0} : le bon de livraison {1} doit être validé.").format(row.idx, row.delivery_note)
			)


def _sales_invoice_movements(doc) -> list[dict]:
	precision = _qty_precision()
	movements = []

	for row in doc.get("items") or []:
		if not _is_stock_item(row.item_code):
			continue
		warehouse = row.warehouse
		if not warehouse and row.dn_detail:
			warehouse = frappe.db.get_value("Delivery Note Item", row.dn_detail, "warehouse")
		if not warehouse:
			continue
		qty = -flt(row.stock_qty or row.qty, precision)
		if qty:
			movements.append(
				{
					"item_code": row.item_code,
					"warehouse": warehouse,
					"stock_uom": row.stock_uom,
					"voucher_detail_no": row.name,
					"delivery_note": row.delivery_note,
					"actual_qty": qty,
				}
			)

	for row in doc.get("packed_items") or []:
		if not _is_stock_item(row.item_code) or not row.warehouse:
			continue
		parent_dn = None
		if row.parent_detail_docname:
			parent_dn = next(
				(
					item.delivery_note
					for item in (doc.get("items") or [])
					if item.name == row.parent_detail_docname
				),
				None,
			)
		qty = -flt(row.qty, precision)
		if qty:
			movements.append(
				{
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"stock_uom": row.uom,
					"voucher_detail_no": row.name,
					"delivery_note": parent_dn,
					"actual_qty": qty,
				}
			)

	return movements


def on_submit_sales_invoice(doc, method=None) -> None:
	"""Post Stock Comptable deduction (or return credit) when Sales Invoice submits."""
	if not is_stock_comptable_enabled():
		return
	if cint(doc.is_debit_note):
		return
	_post_movements(_sales_invoice_movements(doc), doc)


def on_cancel_sales_invoice(doc, method=None) -> None:
	"""Reverse Stock Comptable entries when a Sales Invoice is cancelled."""
	_reverse_voucher_entries(doc)


def on_cancel_delivery_note(doc, method=None) -> None:
	"""Allow cancelling a Delivery Note once any linked Sales Invoice is cancelled."""
	_allow_ignore_linked_entries(doc)


def on_submit_stock_voucher(doc, method=None) -> None:
	"""Mirror non-delivery stock vouchers (Purchase Receipt, Stock Entry) into Stock Comptable."""
	if not is_stock_comptable_enabled():
		return

	sles = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
		fields=["item_code", "warehouse", "stock_uom", "voucher_detail_no", "actual_qty"],
		order_by="creation asc",
	)
	movements = [
		{
			"item_code": sle.item_code,
			"warehouse": sle.warehouse,
			"stock_uom": sle.stock_uom,
			"voucher_detail_no": sle.voucher_detail_no,
			"actual_qty": sle.actual_qty,
		}
		for sle in sles
		if flt(sle.actual_qty)
	]
	_post_movements(movements, doc)


def on_cancel_stock_voucher(doc, method=None) -> None:
	"""Reverse Stock Comptable entries when a Purchase Receipt or Stock Entry is cancelled."""
	_reverse_voucher_entries(doc)


def on_submit_stock_reconciliation(doc, method=None) -> None:
	"""Mirror Stock Reconciliation deltas into Stock Comptable."""
	if not is_stock_comptable_enabled():
		return

	precision = _qty_precision()
	sle_by_detail = {
		sle.voucher_detail_no: sle
		for sle in frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
			fields=["voucher_detail_no", "actual_qty", "stock_uom"],
		)
	}

	movements = []
	for row in doc.get("items") or []:
		if not _is_stock_item(row.item_code) or not row.warehouse:
			continue
		sle = sle_by_detail.get(row.name)
		# Stock Reconciliation sets SLE.actual_qty = 0 for non-batched items and stores the
		# physical count in qty_after_transaction, so compute delta from row.qty - current_qty.
		if sle and flt(sle.actual_qty, precision):
			delta = flt(sle.actual_qty, precision)
		else:
			delta = flt(flt(row.qty, precision) - flt(row.current_qty, precision), precision)
		if delta:
			movements.append(
				{
					"item_code": row.item_code,
					"warehouse": row.warehouse,
					"stock_uom": (sle.stock_uom if sle else None)
					or frappe.get_cached_value("Item", row.item_code, "stock_uom"),
					"voucher_detail_no": row.name,
					"actual_qty": delta,
				}
			)
	_post_movements(movements, doc)
