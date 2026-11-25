import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class TradeImportSettings(Document):
	"""Single settings DocType controlling white-label Trade Import options."""

	def validate(self):
		"""Drop duplicate currency rows and refuse a disabled currency."""
		currencies = []
		for row in self.supplier_currencies:
			if not row.currency or row.currency in currencies:
				continue
			if not frappe.db.get_value("Currency", row.currency, "enabled"):
				frappe.throw(_("La devise {0} est désactivée.").format(row.currency))
			currencies.append(row.currency)
		self.set("supplier_currencies", [{"currency": currency} for currency in currencies])

	def on_update(self):
		"""Create the payable accounts of any newly listed currency, for every existing company.

		Uses the rows of this document, not the cached settings, which are still stale here.
		During a migration or fixture import errors are logged and the warnings are skipped.
		"""
		from trade_import.foreign_payables import (
			_is_unattended,
			ensure_payable_accounts,
			warn_missing_unrealized_exchange_account,
		)

		currencies = [row.currency for row in self.supplier_currencies if row.currency]
		unattended = _is_unattended()
		ensure_payable_accounts(currencies=currencies, strict=not unattended)
		if not unattended:
			warn_missing_unrealized_exchange_account(currencies)
			self._handle_stock_comptable_toggle()

	def _handle_stock_comptable_toggle(self):
		"""Seed the ledger when it is switched on for the first time, and warn when it is switched off.

		Without an opening balance every bin starts at 0, so the first invoice of existing stock
		would be refused (or push the bin negative). A ledger that already has entries is never
		reseeded here; the button on the form does it on request.
		"""
		from trade_import.stock_comptable import enqueue_seeding, ledger_has_entries

		before = self.get_doc_before_save()
		was_on = bool(cint(before.enable_stock_comptable)) if before else False
		is_on = bool(cint(self.enable_stock_comptable))

		if is_on and not was_on:
			if ledger_has_entries():
				frappe.msgprint(
					_(
						"Le stock comptable avait déjà des mouvements. Les mouvements faits pendant la "
						"désactivation ne sont pas rejoués ; utilisez « Initialiser depuis le stock réel » "
						"pour créer les soldes manquants."
					),
					indicator="orange",
				)
			else:
				enqueue_seeding()
				frappe.msgprint(
					_(
						"Initialisation du stock comptable depuis le stock réel lancée en arrière-plan. "
						"Un message vous prévient à la fin."
					),
					indicator="green",
					alert=True,
				)
		elif was_on and not is_on:
			frappe.msgprint(
				_(
					"Les mouvements de stock faits pendant que le stock comptable est désactivé ne seront "
					"pas rejoués à sa réactivation."
				),
				indicator="orange",
			)
