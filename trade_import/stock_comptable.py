import frappe
from erpnext.stock.utils import get_combine_datetime
from frappe import _
from frappe.query_builder import Order
from frappe.utils import cint, flt, nowdate, nowtime

SETTINGS_DOCTYPE = "Trade Import Settings"
ENTRY_DOCTYPE = "Stock Comptable Entry"
BIN_DOCTYPE = "Stock Comptable Bin"
# A named tuple, not `except A, B:`: ruff's formatter drops the parentheses for Python 3.14, and
# the pre-commit syntax hook runs on an older Python that cannot parse that form.
_BIN_INSERT_RACE_ERRORS = (frappe.DuplicateEntryError, frappe.exceptions.UniqueValidationError)


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


def ledger_has_entries() -> bool:
	"""Return True when the Stock Comptable ledger already holds at least one entry (cheap check)."""
	return bool(frappe.get_all(ENTRY_DOCTYPE, pluck="name", limit=1))


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
	"""Lock and return (bin_name, current_qty) for (item_code, warehouse).

	The Bin has a unique key on (item_code, warehouse). A row is locked only once it exists:
	a locking read on a missing row takes a gap lock, and two postings creating the first bin of
	a pair could then deadlock. When two postings do insert at the same moment, the loser is
	rejected by the key and reads the winner's bin.
	"""
	precision = _qty_precision()
	filters = {"item_code": item_code, "warehouse": warehouse}

	def read(lock: bool):
		return frappe.db.get_value(
			BIN_DOCTYPE, filters, ["name", "actual_qty"], as_dict=True, for_update=lock
		)

	existing = read(False) and read(True)
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
	try:
		bin_doc.insert(ignore_permissions=True)
	except _BIN_INSERT_RACE_ERRORS:
		# DuplicateEntryError is a name clash; UniqueValidationError is the (item, warehouse) key.
		# Frappe queued a "must be unique" popup before raising: the loser recovers, so drop it.
		frappe.clear_messages()
		existing = read(True)
		if not existing:
			raise
		return existing.name, flt(existing.actual_qty, precision)
	return bin_doc.name, 0.0


def _group_by_pair(rows: list) -> dict:
	"""Group rows by (item_code, warehouse), keeping each pair's rows in their original order."""
	groups: dict[tuple[str, str], list] = {}
	for row in rows:
		groups.setdefault((row["item_code"], row["warehouse"]), []).append(row)
	return groups


def _post_movements(movements: list[dict], voucher) -> None:
	"""Apply signed Stock Comptable movements atomically for a submitted voucher.

	Bins are locked in sorted (item, warehouse) order, so two vouchers that touch the same
	bins in a different row order cannot deadlock. The negative check looks at the net movement
	of each pair, so a voucher that consumes and produces the same item in one warehouse is not
	refused for its intermediate balance.
	"""
	if not movements:
		return

	precision = _qty_precision()
	allow_negative = bool(cint(_get_settings().allow_negative_stock_comptable))
	posting_date = voucher.get("posting_date") or nowdate()
	posting_time = voucher.get("posting_time") or nowtime()
	posting_datetime = get_combine_datetime(posting_date, posting_time)

	moves = []
	for move in movements:
		qty = flt(move["actual_qty"], precision)
		if qty:
			moves.append({**move, "actual_qty": qty})
	groups = _group_by_pair(moves)

	prev_flag = frappe.flags.in_stock_comptable_posting
	frappe.flags.in_stock_comptable_posting = True
	try:
		for item_code, warehouse in sorted(groups):
			pair_moves = groups[(item_code, warehouse)]
			stock_uom = pair_moves[0].get("stock_uom") or frappe.get_cached_value(
				"Item", item_code, "stock_uom"
			)
			bin_name, current_qty = _get_or_create_bin(item_code, warehouse, voucher.company, stock_uom)

			net = flt(sum(move["actual_qty"] for move in pair_moves), precision)
			if flt(current_qty + net, precision) < 0 and not allow_negative:
				frappe.throw(
					_(
						"Stock comptable insuffisant pour l'article {0} dans l'entrepôt {1} : "
						"disponible {2}, mouvement {3}."
					).format(item_code, warehouse, current_qty, net)
				)

			running_qty = current_qty
			for move in pair_moves:
				running_qty = flt(running_qty + move["actual_qty"], precision)
				frappe.get_doc(
					{
						"doctype": ENTRY_DOCTYPE,
						"item_code": item_code,
						"warehouse": warehouse,
						"company": voucher.company,
						"stock_uom": move.get("stock_uom") or stock_uom,
						"posting_date": posting_date,
						"posting_time": posting_time,
						"posting_datetime": posting_datetime,
						"voucher_type": voucher.doctype,
						"voucher_no": voucher.name,
						"voucher_detail_no": move.get("voucher_detail_no"),
						"delivery_note": move.get("delivery_note"),
						"actual_qty": move["actual_qty"],
						"qty_after_transaction": running_qty,
						"is_cancelled": 0,
					}
				).insert(ignore_permissions=True)

			frappe.db.set_value(BIN_DOCTYPE, bin_name, "actual_qty", running_qty, update_modified=True)
	finally:
		frappe.flags.in_stock_comptable_posting = prev_flag


def _reverse_voucher_entries(voucher) -> None:
	"""Mark voucher entries cancelled and reverse their Stock Comptable Bin impact.

	Same rules as posting: sorted lock order, and the negative check on the net reversal of
	each (item, warehouse) pair.
	"""
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
	groups = _group_by_pair(entries)

	prev_flag = frappe.flags.in_stock_comptable_posting
	frappe.flags.in_stock_comptable_posting = True
	try:
		for item_code, warehouse in sorted(groups):
			pair_entries = groups[(item_code, warehouse)]
			bin_name, current_qty = _get_or_create_bin(
				item_code, warehouse, voucher.company, pair_entries[0].stock_uom
			)
			net = flt(sum(flt(entry.actual_qty, precision) for entry in pair_entries), precision)
			reversed_qty = flt(current_qty - net, precision)
			if reversed_qty < 0 and not allow_negative:
				frappe.throw(
					_(
						"Impossible d'annuler {0} : le stock comptable de {1} dans {2} deviendrait négatif ({3})."
					).format(voucher.name, item_code, warehouse, reversed_qty)
				)

			frappe.db.set_value(BIN_DOCTYPE, bin_name, "actual_qty", reversed_qty, update_modified=True)
			for entry in pair_entries:
				frappe.db.set_value(ENTRY_DOCTYPE, entry.name, "is_cancelled", 1, update_modified=True)
	finally:
		frappe.flags.in_stock_comptable_posting = prev_flag


def validate_sales_invoice(doc, method=None) -> None:
	"""Enforce BL (Delivery Note) before Sales Invoice when configured (D-05).

	On submit, also refuse a stock row that has no warehouse: the Stock Comptable could not be
	reduced and would drift from the real stock without a trace. Drafts can still be saved
	while the user is filling the rows in.
	"""
	if is_stock_comptable_enabled() and not cint(doc.is_debit_note) and doc.get("_action") == "submit":
		_sales_invoice_movements(doc)

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


def _no_warehouse_error(row, item_code: str) -> None:
	frappe.throw(
		_(
			"Ligne {0} : aucun entrepôt pour l'article {1}. Le stock comptable ne peut pas être "
			"mis à jour ; renseignez l'entrepôt de la ligne."
		).format(row.idx, item_code)
	)


def _sales_invoice_movements(doc) -> list[dict]:
	"""Return the Stock Comptable movements of a Sales Invoice. Raise if a stock row has no warehouse.

	A row delivered by the supplier (drop shipping) never leaves one of our warehouses, so it
	moves no stock and needs no warehouse.
	"""
	precision = _qty_precision()
	movements = []

	for row in doc.get("items") or []:
		if cint(row.get("delivered_by_supplier")) or not _is_stock_item(row.item_code):
			continue
		warehouse = row.warehouse
		if not warehouse and row.dn_detail:
			warehouse = frappe.db.get_value("Delivery Note Item", row.dn_detail, "warehouse")
		qty = -flt(row.stock_qty or row.qty, precision)
		if not qty:
			continue
		if not warehouse:
			_no_warehouse_error(row, row.item_code)
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
		if not _is_stock_item(row.item_code):
			continue
		qty = -flt(row.qty, precision)
		if not qty:
			continue
		if not row.warehouse:
			_no_warehouse_error(row, row.item_code)
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


def _stock_ledger_entries_of(doc) -> list:
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0},
		fields=[
			"name",
			"item_code",
			"warehouse",
			"stock_uom",
			"voucher_detail_no",
			"actual_qty",
			"qty_after_transaction",
			"posting_datetime",
			"creation",
		],
		order_by="posting_datetime asc, creation asc",
	)


def on_submit_stock_voucher(doc, method=None) -> None:
	"""Mirror vouchers that move stock outside the Delivery Note into Stock Comptable.

	Covers Purchase Receipt, Stock Entry, Purchase Invoice with update stock, Subcontracting
	Receipt and Asset Capitalization. The movements are read from the Stock Ledger Entries the
	voucher posted, so a voucher that moves no stock (a Purchase Invoice without update stock)
	posts nothing.
	"""
	if not is_stock_comptable_enabled():
		return

	movements = [
		{
			"item_code": sle.item_code,
			"warehouse": sle.warehouse,
			"stock_uom": sle.stock_uom,
			"voucher_detail_no": sle.voucher_detail_no,
			"actual_qty": sle.actual_qty,
		}
		for sle in _stock_ledger_entries_of(doc)
		if flt(sle.actual_qty)
	]
	_post_movements(movements, doc)


def on_cancel_stock_voucher(doc, method=None) -> None:
	"""Reverse Stock Comptable entries when a mirrored stock voucher is cancelled."""
	_reverse_voucher_entries(doc)


def _quantity_before(sle) -> float:
	"""Return the item-warehouse quantity just before a Stock Ledger Entry, 0 if it is the first."""
	ledger = frappe.qb.DocType("Stock Ledger Entry")
	previous = (
		frappe.qb.from_(ledger)
		.select(ledger.qty_after_transaction)
		.where(ledger.item_code == sle.item_code)
		.where(ledger.warehouse == sle.warehouse)
		.where(ledger.is_cancelled == 0)
		.where(ledger.name != sle.name)
		.where(
			(ledger.posting_datetime < sle.posting_datetime)
			| ((ledger.posting_datetime == sle.posting_datetime) & (ledger.creation < sle.creation))
		)
		.orderby(ledger.posting_datetime, order=Order.desc)
		.orderby(ledger.creation, order=Order.desc)
		.limit(1)
		.run()
	)
	return flt(previous[0][0]) if previous else 0.0


def _row_has_dimensions(row) -> bool:
	"""True when a reconciliation row carries an inventory dimension value."""
	from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions

	return any(row.get(dimension.get("fieldname")) for dimension in get_inventory_dimensions())


def _dimension_row_deltas(doc, precision: int) -> dict:
	"""Return {(item, warehouse): counted - current} for rows with inventory dimensions.

	For those rows ERPNext v16 writes the counted quantity (not the change) as the entry
	quantity and a running balance of 0, so the entries cannot give the delta. ERPNext
	recomputes `current_qty` when the document is submitted, so counted minus current is the
	change. Batch and serial rows are left to the entries.
	"""
	deltas: dict[tuple[str, str], float] = {}
	for row in doc.get("items") or []:
		if row.get("batch_no") or row.get("serial_and_batch_bundle") or not _row_has_dimensions(row):
			continue
		key = (row.item_code, row.warehouse)
		deltas[key] = flt(
			deltas.get(key, 0) + flt(row.qty, precision) - flt(row.current_qty, precision), precision
		)
	return deltas


def _reconciliation_movements(doc) -> list[dict]:
	"""Return one movement per (item, warehouse) from the Stock Ledger Entries of a reconciliation.

	The delta is the sum of the entries' quantities. When that sum is zero (a counted quantity
	stored only in qty_after_transaction), it is the new quantity minus the quantity before the
	voucher. Summing per pair, not per row, keeps batch and serial rows that post several entries.
	Rows with inventory dimensions use counted minus current quantity instead.
	"""
	precision = _qty_precision()
	groups = _group_by_pair([dict(sle) for sle in _stock_ledger_entries_of(doc)])
	dimension_deltas = _dimension_row_deltas(doc, precision)

	movements = []
	for (item_code, warehouse), entries in groups.items():
		if (item_code, warehouse) in dimension_deltas:
			delta = dimension_deltas[(item_code, warehouse)]
		else:
			delta = flt(sum(flt(entry["actual_qty"], precision) for entry in entries), precision)
			if not delta:
				delta = flt(
					flt(entries[-1]["qty_after_transaction"], precision)
					- _quantity_before(frappe._dict(entries[0])),
					precision,
				)
		if delta:
			movements.append(
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"stock_uom": entries[0]["stock_uom"],
					"voucher_detail_no": entries[0]["voucher_detail_no"],
					"actual_qty": delta,
				}
			)
	return movements


def on_submit_stock_reconciliation(doc, method=None) -> None:
	"""Mirror quantity changes of a Stock Reconciliation into Stock Comptable."""
	if not is_stock_comptable_enabled():
		return
	# An "Adjustment Entry" re-aligns balances and valuation. ERPNext v16 writes several entries
	# per row for it, which are not quantity deltas, so it is left out. Any real quantity drift
	# stays visible in the Stock vs Stock Comptable report.
	if doc.get("purpose") == "Adjustment Entry":
		return
	_post_movements(_reconciliation_movements(doc), doc)


def initialise_missing_bins(item_code: str | None = None) -> dict:
	"""Seed a Stock Comptable bin from the real stock for every positive pair that has none yet.

	Idempotent: a pair that already has a bin is left untouched, so running it twice changes
	nothing. A pair with negative real stock is not seeded (it would bypass the negative-stock
	setting) and is counted in `skipped_negative`. Each seeded pair gets one opening entry,
	voucher Trade Import Settings. Run it as a background job for a large stock: the job commits
	once, when it ends.
	"""
	precision = _qty_precision()
	extra = {"item_code": item_code} if item_code else {}
	real_bins = frappe.get_all(
		"Bin",
		filters={"actual_qty": [">", 0], **extra},
		fields=["item_code", "warehouse", "actual_qty", "stock_uom"],
		order_by="item_code asc, warehouse asc",
	)
	skipped_negative = frappe.db.count("Bin", {"actual_qty": ["<", 0], **extra})
	existing = {
		(b.item_code, b.warehouse) for b in frappe.get_all(BIN_DOCTYPE, fields=["item_code", "warehouse"])
	}
	posting_date, posting_time = nowdate(), nowtime()
	posting_datetime = get_combine_datetime(posting_date, posting_time)

	created = 0
	prev_flag = frappe.flags.in_stock_comptable_posting
	frappe.flags.in_stock_comptable_posting = True
	try:
		for real in real_bins:
			if (real.item_code, real.warehouse) in existing:
				continue
			company = frappe.get_cached_value("Warehouse", real.warehouse, "company")
			bin_name, current_qty = _get_or_create_bin(
				real.item_code, real.warehouse, company, real.stock_uom
			)
			if current_qty:
				continue
			qty = flt(real.actual_qty, precision)
			frappe.db.set_value(BIN_DOCTYPE, bin_name, "actual_qty", qty, update_modified=True)
			frappe.get_doc(
				{
					"doctype": ENTRY_DOCTYPE,
					"item_code": real.item_code,
					"warehouse": real.warehouse,
					"company": company,
					"stock_uom": real.stock_uom,
					"posting_date": posting_date,
					"posting_time": posting_time,
					"posting_datetime": posting_datetime,
					"voucher_type": SETTINGS_DOCTYPE,
					"voucher_no": SETTINGS_DOCTYPE,
					"actual_qty": qty,
					"qty_after_transaction": qty,
					"is_cancelled": 0,
				}
			).insert(ignore_permissions=True)
			created += 1
	finally:
		frappe.flags.in_stock_comptable_posting = prev_flag
	return {
		"created": created,
		"already_present": len(real_bins) - created,
		"skipped_negative": skipped_negative,
	}


def seed_in_background() -> None:
	"""Background job: seed the ledger from the real stock, then tell the user who asked for it."""
	result = initialise_missing_bins()
	frappe.publish_realtime(
		"msgprint",
		{
			"title": _("Stock comptable"),
			"message": _(
				"Initialisation terminée. Soldes créés : {0}. Déjà présents : {1}. "
				"Stock réel négatif ignoré : {2}."
			).format(result["created"], result["already_present"], result["skipped_negative"]),
		},
		user=frappe.session.user,
	)


def enqueue_seeding() -> None:
	"""Queue the seeding on the long queue: thousands of pairs do not belong in a web request."""
	frappe.enqueue(
		"trade_import.stock_comptable.seed_in_background",
		queue="long",
		timeout=3600,
		enqueue_after_commit=True,
	)


@frappe.whitelist()
def initialise_from_real_stock() -> dict:
	"""Seed the Stock Comptable from the real stock (button on Trade Import Settings)."""
	frappe.only_for(("System Manager", "Stock Manager"))
	if not is_stock_comptable_enabled():
		frappe.throw(_("Activez d'abord le stock comptable parallèle."))
	enqueue_seeding()
	return {"queued": True}
