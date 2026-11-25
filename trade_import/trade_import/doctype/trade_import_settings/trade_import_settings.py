import frappe
from frappe import _
from frappe.model.document import Document


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
