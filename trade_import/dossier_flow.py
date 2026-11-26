"""Dossier Importation: the logic behind one shipment (CHW-106).

Pure functions at the top (no database), database functions below. Spec:
docs/specs/2026-11-26-dossier-importation-design.md
"""

import json

import frappe
from frappe import _
from frappe.utils import flt

STATUS_DRAFT = "Brouillon"
STATUS_ORDERED = "Commandé"
STATUS_IN_TRANSIT = "En transit"
STATUS_IN_CUSTOMS = "En dédouanement"
STATUS_RECEIVED = "Réceptionné"
STATUS_FINALISED = "Coûts finalisés"
STATUS_CLOSED = "Clôturé"

FROM_LIST = "Liste de colisage"
FROM_ITEM = "Fiche article"

# Set by the cost steps (later PRs), never taken away by the dates and receipts.
_LATER_STATUSES = (STATUS_FINALISED, STATUS_CLOSED)


def compute_status(
	has_lines: bool, departed: bool, arrived: bool, all_received: bool, current: str | None
) -> str:
	"""The status the dates and receipts give. A finalised or closed Dossier keeps its status."""
	if current in _LATER_STATUSES:
		return current
	if all_received and has_lines:
		return STATUS_RECEIVED
	if arrived:
		return STATUS_IN_CUSTOMS
	if departed:
		return STATUS_IN_TRANSIT
	return STATUS_ORDERED if has_lines else STATUS_DRAFT


def unit_weight_kg(weight_per_unit: float, weight_uom: str | None) -> float:
	"""The item card's unit weight, when it is in kilograms (or has no unit set)."""
	return flt(weight_per_unit) if weight_uom in (None, "", "Kg") else 0.0


def packing_values(
	source: str | None,
	weight: float,
	volume: float,
	stock_qty: float,
	unit_weight: float,
	unit_volume: float,
) -> tuple[float, float, str]:
	"""Weight (kg), volume (m³) and where they come from, for one item line.

	Values entered from the packing list are kept; an empty one is filled from the item card times
	the stock quantity. A line marked "Fiche article" is filled from the item card again.
	"""
	weight, volume = flt(weight), flt(volume)
	if source == FROM_ITEM:
		weight = volume = 0.0
	entered = bool(weight or volume)
	if not weight:
		weight = flt(unit_weight) * flt(stock_qty)
	if not volume:
		volume = flt(unit_volume) * flt(stock_qty)
	if entered:
		return weight, volume, FROM_LIST
	return weight, volume, FROM_ITEM if (weight or volume) else ""


# ---------------------------------------------------------------------------------------------
# Validation of a Dossier
# ---------------------------------------------------------------------------------------------


def prepare_lines(doc) -> None:
	"""Quantities, values and packing figures of every item line."""
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	codes = {row.item_code for row in doc.articles if row.item_code}
	items = {
		item.name: item
		for item in frappe.get_all(
			"Item",
			filters={"name": ("in", list(codes))},
			fields=["name", "weight_per_unit", "weight_uom", "volume_par_unite"],
		)
	}
	for row in doc.articles:
		previous_stock_qty = flt(row.stock_qty)
		row.stock_qty = flt(flt(row.qty) * flt(row.conversion_factor or 1), row.precision("stock_qty"))
		row.amount = flt(flt(row.qty) * flt(row.rate), row.precision("amount"))
		if row.currency == company_currency:
			row.exchange_rate = 1.0
		elif not row.currency or not flt(row.exchange_rate):
			frappe.throw(
				_("Ligne {0} : taux de change requis pour la devise {1}.").format(
					row.idx, row.currency or "?"
				),
				title=_("Taux de change manquant"),
			)
		row.valeur_mad = flt(row.amount * row.exchange_rate, row.precision("valeur_mad"))
		item = items.get(row.item_code)
		unit_weight = unit_weight_kg(item.weight_per_unit, item.weight_uom) if item else 0
		unit_volume = flt(item.volume_par_unite) if item else 0
		if item and row.source_colisage == FROM_ITEM:
			# A figure that no longer matches the item card was typed by the user: keep it.
			weight_precision, volume_precision = row.precision("poids_kg"), row.precision("volume_m3")
			if flt(row.poids_kg, weight_precision) != flt(
				unit_weight * previous_stock_qty, weight_precision
			) or flt(row.volume_m3, volume_precision) != flt(
				unit_volume * previous_stock_qty, volume_precision
			):
				row.source_colisage = FROM_LIST
		row.poids_kg, row.volume_m3, row.source_colisage = packing_values(
			row.source_colisage,
			row.poids_kg,
			row.volume_m3,
			row.stock_qty,
			unit_weight,
			unit_volume,
		)
		row.poids_kg = flt(row.poids_kg, row.precision("poids_kg"))
		row.volume_m3 = flt(row.volume_m3, row.precision("volume_m3"))


def set_totals(doc) -> None:
	doc.total_valeur_mad = flt(
		sum(flt(row.valeur_mad) for row in doc.articles), doc.precision("total_valeur_mad")
	)
	doc.total_poids_kg = flt(sum(flt(row.poids_kg) for row in doc.articles), doc.precision("total_poids_kg"))
	doc.total_volume_m3 = flt(
		sum(flt(row.volume_m3) for row in doc.articles), doc.precision("total_volume_m3")
	)


def status_of(doc) -> str:
	rows = doc.articles
	all_received = bool(rows) and all(flt(row.received_qty, 6) >= flt(row.qty, 6) for row in rows)
	return compute_status(
		bool(rows),
		bool(doc.date_depart_reelle),
		bool(doc.date_arrivee_reelle),
		all_received,
		doc.status,
	)


def available_qty(po_items: list[str], exclude_dossier: str | None = None) -> dict[str, float]:
	"""Quantity of each purchase order line that no Dossier has taken yet.

	Received outside any Dossier counts as taken; a Dossier line counts in full, received or not.
	`exclude_dossier` leaves one Dossier's own lines out, to check that Dossier on save.
	"""
	if not po_items:
		return {}
	order_rows = frappe.get_all(
		"Purchase Order Item",
		filters={"name": ("in", po_items)},
		fields=["name", "qty", "received_qty"],
	)
	dossier_rows = frappe.get_all(
		"Dossier Importation Article",
		filters={"purchase_order_item": ("in", po_items)},
		fields=["parent", "purchase_order_item", "qty", "received_qty"],
	)
	result = {}
	for order_row in order_rows:
		mine = [r for r in dossier_rows if r.purchase_order_item == order_row.name]
		received_in_dossiers = sum(flt(r.received_qty) for r in mine)
		taken = sum(flt(r.qty) for r in mine if r.parent != exclude_dossier)
		received_outside = max(0, flt(order_row.received_qty) - received_in_dossiers)
		result[order_row.name] = flt(flt(order_row.qty) - received_outside - taken, 6)
	return result


@frappe.whitelist()
def get_open_order_lines(company: str, supplier: str | None = None) -> list[dict]:
	"""Purchase order lines still free to put in a Dossier, with the quantity left on each."""
	frappe.has_permission("Purchase Order", "read", throw=True)
	order = frappe.qb.DocType("Purchase Order")
	line = frappe.qb.DocType("Purchase Order Item")
	query = (
		frappe.qb.from_(line)
		.join(order)
		.on(line.parent == order.name)
		.select(
			line.name.as_("purchase_order_item"),
			line.parent.as_("purchase_order"),
			order.supplier,
			order.currency,
			order.conversion_rate,
			order.transaction_date,
			line.item_code,
			line.item_name,
			line.uom,
			line.conversion_factor,
			line.rate,
			line.qty,
			line.received_qty,
		)
		.where(
			(order.docstatus == 1)
			& (order.company == company)
			& (order.status.notin(["Closed", "On Hold", "Completed", "Cancelled"]))
			& (line.received_qty < line.qty)
		)
		.orderby(order.transaction_date)
	)
	if supplier:
		query = query.where(order.supplier == supplier)
	rows = query.run(as_dict=True)
	left = available_qty([row.purchase_order_item for row in rows])
	open_lines = []
	for row in rows:
		remaining = left.get(row.purchase_order_item, 0)
		if remaining > 0:
			row["qty"] = remaining
			open_lines.append(row)
	return open_lines


def check_lines(doc) -> None:
	"""Every line belongs to the company, and no purchase order line is over-allocated."""
	rows = [row for row in doc.articles if row.purchase_order_item]
	if not rows:
		return
	parents = dict(
		frappe.get_all(
			"Purchase Order Item",
			filters={"name": ("in", list({row.purchase_order_item for row in rows}))},
			fields=["name", "parent"],
			as_list=True,
		)
	)
	orders = {
		order.name: order
		for order in frappe.get_all(
			"Purchase Order",
			filters={"name": ("in", list(set(parents.values())))},
			fields=["name", "company", "docstatus"],
		)
	}
	wanted: dict[str, float] = {}
	for row in rows:
		real_order = parents.get(row.purchase_order_item)
		if not real_order:
			frappe.throw(
				_("Ligne {0} : la ligne de commande {1} n'existe pas.").format(
					row.idx, frappe.bold(row.purchase_order_item)
				)
			)
		if row.purchase_order != real_order:
			frappe.throw(
				_("Ligne {0} : la ligne de commande {1} appartient à la commande {2}, pas à {3}.").format(
					row.idx,
					frappe.bold(row.purchase_order_item),
					frappe.bold(real_order),
					frappe.bold(row.purchase_order),
				)
			)
		order = orders[real_order]
		if order.company != doc.company:
			frappe.throw(
				_("Ligne {0} : la commande {1} n'appartient pas à la société {2}.").format(
					row.idx, frappe.bold(real_order), frappe.bold(doc.company)
				)
			)
		if order.docstatus != 1:
			frappe.throw(
				_("Ligne {0} : la commande {1} n'est pas validée.").format(row.idx, frappe.bold(real_order))
			)
		wanted[row.purchase_order_item] = wanted.get(row.purchase_order_item, 0) + flt(row.qty)
	left = available_qty(list(wanted), exclude_dossier=None if doc.is_new() else doc.name)
	for po_item, qty in wanted.items():
		if flt(qty, 6) > flt(left.get(po_item, 0), 6):
			frappe.throw(
				_("La quantité de la ligne de commande {0} dépasse ce qui reste à expédier ({1}).").format(
					frappe.bold(po_item), left.get(po_item, 0)
				)
			)


@frappe.whitelist()
def add_order_lines(dossier: str, po_items: list[str] | str) -> None:
	"""Put the chosen purchase order lines in the Dossier, with the quantity left on each."""
	doc = frappe.get_doc("Dossier Importation", dossier)
	doc.check_permission("write")
	if isinstance(po_items, str):
		po_items = json.loads(po_items)
	open_lines = {line.purchase_order_item: line for line in get_open_order_lines(doc.company)}
	for name in po_items:
		line = open_lines.get(name)
		if not line:
			frappe.throw(_("La ligne {0} n'est plus disponible.").format(frappe.bold(name)))
		doc.append(
			"articles",
			{
				"purchase_order": line.purchase_order,
				"purchase_order_item": line.purchase_order_item,
				"supplier": line.supplier,
				"item_code": line.item_code,
				"item_name": line.item_name,
				"qty": line.qty,
				"uom": line.uom,
				"conversion_factor": line.conversion_factor,
				"currency": line.currency,
				"rate": line.rate,
				"exchange_rate": line.conversion_rate,
			},
		)
	doc.save()
	# The link on the order is a convenience for the connections panel; the item lines are the record.
	for name in po_items:
		frappe.db.set_value(
			"Purchase Order Item", name, "dossier_importation", doc.name, update_modified=False
		)
		frappe.db.set_value(
			"Purchase Order",
			open_lines[name].purchase_order,
			"dossier_importation",
			doc.name,
			update_modified=False,
		)


@frappe.whitelist()
def create_receipts(dossier: str) -> list[str]:
	"""Draft purchase receipts for what is not received yet: one per supplier and currency."""
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	doc = frappe.get_doc("Dossier Importation", dossier)
	doc.check_permission("write")
	drafts = frappe.get_all(
		"Purchase Receipt", filters={"dossier_importation": doc.name, "docstatus": 0}, pluck="name"
	)
	if drafts:
		frappe.throw(
			_("Un brouillon de réception existe déjà pour ce dossier : {0}.").format(", ".join(drafts))
		)
	# (supplier, currency) -> purchase order -> purchase order line -> quantity to receive
	pending: dict[tuple, dict[str, dict[str, float]]] = {}
	for row in doc.articles:
		to_receive = flt(row.qty) - flt(row.received_qty)
		if to_receive > 0 and row.purchase_order_item:
			orders = pending.setdefault((row.supplier, row.currency), {})
			orders.setdefault(row.purchase_order, {})[row.purchase_order_item] = to_receive
	if not pending:
		frappe.throw(_("Rien à réceptionner : toutes les lignes sont reçues."))
	created = []
	for orders in pending.values():
		receipt = None
		for order, lines in orders.items():
			receipt = make_purchase_receipt(
				order, target_doc=receipt, args={"filtered_children": list(lines)}
			)
		wanted = {name: qty for lines in orders.values() for name, qty in lines.items()}
		receipt.dossier_importation = doc.name
		for item in receipt.items:
			item.qty = wanted[item.purchase_order_item]
			item.received_qty = item.qty  # nothing rejected: received = accepted
		receipt.insert()
		created.append(receipt.name)
	return created


def refresh_from_receipts(dossier: str, ignore_receipt: str | None = None) -> None:
	"""Received quantities, receipt references and rates of the Dossier lines, from the receipts.

	Only submitted receipts linked to the Dossier count. `ignore_receipt` leaves one out, for the
	receipt being cancelled.
	"""
	doc = frappe.get_doc("Dossier Importation", dossier)
	po_items = [row.purchase_order_item for row in doc.articles if row.purchase_order_item]
	if not po_items:
		return
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	receipt = frappe.qb.DocType("Purchase Receipt")
	item = frappe.qb.DocType("Purchase Receipt Item")
	query = (
		frappe.qb.from_(item)
		.join(receipt)
		.on(item.parent == receipt.name)
		.select(
			item.purchase_order_item,
			item.qty,
			receipt.name.as_("receipt"),
			receipt.conversion_rate,
		)
		.where(
			(receipt.docstatus == 1)
			& (receipt.dossier_importation == dossier)
			& (item.purchase_order_item.isin(po_items))
		)
		.orderby(receipt.posting_date)
		.orderby(receipt.creation)
	)
	if ignore_receipt:
		query = query.where(receipt.name != ignore_receipt)
	received: dict[str, list] = {}
	for entry in query.run(as_dict=True):
		received.setdefault(entry.purchase_order_item, []).append(entry)
	order_rates = dict(
		frappe.get_all(
			"Purchase Order",
			filters={"name": ("in", list({row.purchase_order for row in doc.articles}))},
			fields=["name", "conversion_rate"],
			as_list=True,
		)
	)
	for row in doc.articles:
		entries = received.get(row.purchase_order_item, [])
		row.received_qty = sum(flt(entry.qty) for entry in entries)
		# available_qty reads the stored rows: they must agree with the receipts before validate
		frappe.db.set_value(row.doctype, row.name, "received_qty", row.received_qty, update_modified=False)
		row.purchase_receipt = entries[-1].receipt if entries else None
		if row.currency != company_currency:
			row.exchange_rate = (
				flt(entries[-1].conversion_rate) if entries else flt(order_rates.get(row.purchase_order))
			)
	doc.save(ignore_permissions=True)


def receipt_on_submit(doc, method=None) -> None:
	if doc.get("dossier_importation"):
		refresh_from_receipts(doc.dossier_importation)


def receipt_on_cancel(doc, method=None) -> None:
	if doc.get("dossier_importation"):
		refresh_from_receipts(doc.dossier_importation, ignore_receipt=doc.name)
