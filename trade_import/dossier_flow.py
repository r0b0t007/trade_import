"""Dossier Importation: the logic behind one shipment (CHW-106).

Pure functions at the top (no database), database functions below. Spec:
docs/specs/2026-11-26-dossier-importation-design.md
"""

import frappe
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
		row.stock_qty = flt(flt(row.qty) * flt(row.conversion_factor or 1), row.precision("stock_qty"))
		row.amount = flt(flt(row.qty) * flt(row.rate), row.precision("amount"))
		row.exchange_rate = 1.0 if row.currency == company_currency else flt(row.exchange_rate) or 1.0
		row.valeur_mad = flt(row.amount * row.exchange_rate, row.precision("valeur_mad"))
		item = items.get(row.item_code)
		row.poids_kg, row.volume_m3, row.source_colisage = packing_values(
			row.source_colisage,
			row.poids_kg,
			row.volume_m3,
			row.stock_qty,
			unit_weight_kg(item.weight_per_unit, item.weight_uom) if item else 0,
			flt(item.volume_par_unite) if item else 0,
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
