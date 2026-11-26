"""Dossier Importation: the logic behind one shipment (CHW-106).

Pure functions at the top (no database), database functions below. Spec:
docs/specs/2026-11-26-dossier-importation-design.md
"""

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
