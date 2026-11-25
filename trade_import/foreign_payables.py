"""Supplier payable accounts in foreign currencies.

ERPNext refuses a foreign-currency purchase invoice against a payable account kept in the company
currency. Importers pay suppliers from a bank account in the company currency, so only the
supplier side needs one payable account per foreign currency. The list of currencies is a tenant
value in Trade Import Settings and applies to every company. The accounts are created once a
company has a chart of accounts (after the setup wizard, when a company is saved, when the
settings are saved, or when a supplier needs one).

Accounts are never deleted or disabled when a currency leaves the list.
"""

import frappe
from frappe import _

SETTINGS_DOCTYPE = "Trade Import Settings"


def get_configured_currencies() -> list[str]:
	"""Return the foreign currencies listed in Trade Import Settings."""
	rows = frappe.get_cached_doc(SETTINGS_DOCTYPE).get("supplier_currencies") or []
	return list(dict.fromkeys(row.currency for row in rows if row.currency))


def _companies() -> list[str]:
	return frappe.get_all("Company", pluck="name")


def _is_unattended() -> bool:
	"""True while a setup, import or migration runs: errors are logged there, never raised."""
	flags = frappe.flags
	return bool(flags.in_import or flags.in_fixtures or flags.in_migrate or flags.in_install)


def get_payable_account_name(company: str, currency: str) -> str | None:
	"""Return the oldest active leaf payable account of a company in the given currency."""
	return frappe.db.get_value(
		"Account",
		{
			"company": company,
			"account_type": "Payable",
			"account_currency": currency,
			"is_group": 0,
			"disabled": 0,
		},
		"name",
		order_by="creation asc",
	)


def _payable_parent(company: str) -> str | None:
	default_payable = frappe.get_cached_value("Company", company, "default_payable_account")
	if not default_payable:
		return None
	return frappe.db.get_value("Account", default_payable, "parent_account")


def ensure_payable_account(company: str, currency: str) -> str | None:
	"""Return the payable account for (company, currency), creating it when missing.

	Returns None when the company has no default payable account yet, so nothing is created
	before the chart of accounts exists. Raises a clear error when an account with the standard
	name already exists but cannot be used (disabled, another currency or another type).
	"""
	existing = get_payable_account_name(company, currency)
	if existing:
		return existing

	parent = _payable_parent(company)
	if not parent:
		return None

	account_name = f"Fournisseurs {currency}"
	full_name = f"{account_name} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if frappe.db.exists("Account", full_name):
		frappe.throw(
			_(
				"Le compte {0} existe déjà mais n'est pas un compte fournisseur actif en {1}. "
				"Activez-le, renommez-le, ou créez à la main le compte fournisseur en {1} pour la société {2}."
			).format(full_name, currency, company)
		)

	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"company": company,
			"parent_account": parent,
			"is_group": 0,
			"account_type": "Payable",
			"account_currency": currency,
		}
	)
	account.insert(ignore_permissions=True)
	return account.name


def ensure_payable_accounts(
	company: str | None = None, currencies: list[str] | None = None, strict: bool = True
) -> list[str]:
	"""Create the missing payable accounts for every configured currency. Return the new names.

	Does nothing on a site without a company, or when no currency is configured. A currency equal
	to the company currency is skipped, since the standard payable account already covers it.
	With strict=False an error on one account is logged and the others are still created.
	"""
	if currencies is None:
		currencies = get_configured_currencies()
	if not currencies:
		return []

	created, no_chart = [], []
	for company_name in [company] if company else _companies():
		company_currency = frappe.get_cached_value("Company", company_name, "default_currency")
		for currency in currencies:
			if currency == company_currency or get_payable_account_name(company_name, currency):
				continue
			try:
				name = ensure_payable_account(company_name, currency)
			except Exception:
				if strict:
					raise
				frappe.log_error(title=f"Compte fournisseur {currency} non créé pour {company_name}")
				continue
			if name:
				created.append(name)
			elif company_name not in no_chart:
				no_chart.append(company_name)

	if strict and no_chart:
		frappe.msgprint(
			_(
				"Aucun compte fournisseur par défaut pour : {0}. Aucun compte en devise n'a été créé pour cette société."
			).format(", ".join(no_chart)),
			indicator="orange",
			alert=True,
		)
	return created


def warn_missing_unrealized_exchange_account(currencies: list[str]) -> None:
	"""Tell the user the year-end revaluation of open foreign payables will not work yet.

	The account to use is an accounting decision, so it is never set automatically.
	"""
	if not currencies:
		return
	missing = [
		company
		for company in _companies()
		if not frappe.get_cached_value("Company", company, "unrealized_exchange_gain_loss_account")
	]
	if missing:
		frappe.msgprint(
			_(
				"Le compte de gain/perte de change non réalisé n'est pas défini pour : {0}. "
				"La réévaluation de fin de période des dettes en devises échouera tant que "
				"ce compte n'est pas choisi avec le comptable."
			).format(", ".join(missing)),
			indicator="orange",
		)


def after_setup_wizard(setup_args=None) -> None:
	"""Create the payable accounts once the setup wizard has created the company.

	An error here is logged and never blocks the wizard: the accounts are also created
	when a company or the settings are saved, or when a supplier needs one.
	"""
	ensure_payable_accounts(strict=False)


def company_on_update(doc, method=None) -> None:
	"""Create the payable accounts for a company created or saved after the settings were filled."""
	ensure_payable_accounts(company=doc.name, strict=False)


def attach_party_account(doc, method=None) -> None:
	"""Give a supplier the payable account of its billing currency, per company (before_validate).

	Runs before ERPNext's own checks, so they apply to the row set here. An account the user
	chose is kept. A row that points to another listed foreign currency is replaced, so changing
	the billing currency from USD to EUR needs no manual cleanup. A currency that is not listed
	in Trade Import Settings is left alone, so ERPNext's own checks still apply.
	"""
	currency = doc.get("default_currency")
	configured = get_configured_currencies()
	if not currency or currency not in configured:
		return

	rows = {row.company: row for row in doc.get("accounts") or []}
	for company in _companies():
		if frappe.get_cached_value("Company", company, "default_currency") == currency:
			continue
		row = rows.get(company)
		if row:
			row_currency = frappe.get_cached_value("Account", row.account, "account_currency")
			if row_currency == currency or row_currency not in configured:
				continue
		account = ensure_payable_account(company, currency)
		if not account:
			continue
		if row:
			row.account = account
		else:
			doc.append("accounts", {"company": company, "account": account})
