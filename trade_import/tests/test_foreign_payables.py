from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from trade_import import foreign_payables
from trade_import.tests.test_stock_comptable import _ensure_test_master_data

CURRENCY = "CHF"
OTHER_CURRENCY = "EUR"


def _configured(*currencies):
	return patch.object(foreign_payables, "get_configured_currencies", return_value=list(currencies))


class TestForeignPayables(IntegrationTestCase):
	def setUp(self):
		self.company = _ensure_test_master_data()[0]
		self.company_currency = frappe.db.get_value("Company", self.company, "default_currency")
		self.assertNotEqual(CURRENCY, self.company_currency)
		for currency in (CURRENCY, OTHER_CURRENCY):
			if not frappe.db.exists("Currency", currency):
				frappe.get_doc({"doctype": "Currency", "currency_name": currency, "enabled": 1}).insert(
					ignore_permissions=True
				)

	def tearDown(self):
		# Tests in a class share one transaction; roll back so each test starts clean.
		frappe.db.rollback()

	def _account(self, currency=CURRENCY):
		return foreign_payables.get_payable_account_name(self.company, currency)

	def _supplier(self, currency, accounts=None):
		return frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "_Test FX Supplier",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
				"default_currency": currency,
				"accounts": accounts or [],
			}
		)

	# --- account creation -------------------------------------------------------------------

	def test_creates_payable_account_in_currency(self):
		self.assertIsNone(self._account())
		created = foreign_payables.ensure_payable_accounts(company=self.company, currencies=[CURRENCY])
		self.assertEqual(len(created), 1)
		account = frappe.get_doc("Account", created[0])
		self.assertEqual(account.account_type, "Payable")
		self.assertEqual(account.account_currency, CURRENCY)
		self.assertEqual(account.company, self.company)
		self.assertEqual(account.is_group, 0)

	def test_is_idempotent(self):
		foreign_payables.ensure_payable_accounts(company=self.company, currencies=[CURRENCY])
		again = foreign_payables.ensure_payable_accounts(company=self.company, currencies=[CURRENCY])
		self.assertEqual(again, [])

	def test_skips_company_currency_and_empty_list(self):
		self.assertEqual(
			foreign_payables.ensure_payable_accounts(
				company=self.company, currencies=[self.company_currency]
			),
			[],
		)
		self.assertEqual(foreign_payables.ensure_payable_accounts(company=self.company, currencies=[]), [])

	def test_does_nothing_without_a_company(self):
		with patch.object(foreign_payables, "_companies", return_value=[]):
			self.assertEqual(foreign_payables.ensure_payable_accounts(currencies=[CURRENCY]), [])

	def test_company_without_default_payable_account_is_skipped(self):
		with patch.object(foreign_payables, "_payable_parent", return_value=None):
			self.assertEqual(
				foreign_payables.ensure_payable_accounts(company=self.company, currencies=[CURRENCY]), []
			)
		self.assertIsNone(self._account())

	def test_disabled_account_is_ignored_and_the_name_clash_is_reported(self):
		name = foreign_payables.ensure_payable_account(self.company, CURRENCY)
		frappe.db.set_value("Account", name, "disabled", 1)
		self.assertIsNone(self._account())
		with self.assertRaises(frappe.ValidationError):
			foreign_payables.ensure_payable_accounts(company=self.company, currencies=[CURRENCY])
		# Unattended runs (wizard, migrate) log the error and never raise.
		with patch.object(foreign_payables.frappe, "log_error") as log_error:
			created = foreign_payables.ensure_payable_accounts(
				company=self.company, currencies=[CURRENCY], strict=False
			)
		self.assertEqual(created, [])
		log_error.assert_called_once()

	def test_after_setup_wizard_creates_accounts(self):
		with _configured(CURRENCY):
			foreign_payables.after_setup_wizard({})
		self.assertIsNotNone(self._account())

	def test_after_setup_wizard_never_raises(self):
		with (
			_configured(CURRENCY),
			patch.object(
				foreign_payables, "ensure_payable_account", side_effect=frappe.ValidationError("boom")
			),
			patch.object(foreign_payables.frappe, "log_error"),
		):
			foreign_payables.after_setup_wizard({})

	def test_company_hook_creates_accounts(self):
		with _configured(CURRENCY):
			foreign_payables.company_on_update(frappe.get_doc("Company", self.company))
		self.assertIsNotNone(self._account())

	# --- supplier ---------------------------------------------------------------------------

	def test_supplier_gets_the_account_of_its_billing_currency(self):
		with _configured(CURRENCY):
			supplier = self._supplier(CURRENCY)
			supplier.insert(ignore_permissions=True)
		rows = {row.company: row.account for row in supplier.accounts}
		self.assertEqual(rows[self.company], self._account())

	def test_supplier_keeps_the_account_the_user_chose(self):
		chosen = foreign_payables.ensure_payable_account(self.company, CURRENCY)
		with _configured(CURRENCY):
			supplier = self._supplier(CURRENCY, [{"company": self.company, "account": chosen}])
			supplier.insert(ignore_permissions=True)
		self.assertEqual([row.account for row in supplier.accounts if row.company == self.company], [chosen])

	def test_changing_the_billing_currency_replaces_the_automatic_row(self):
		with _configured(CURRENCY, OTHER_CURRENCY):
			supplier = self._supplier(CURRENCY)
			supplier.insert(ignore_permissions=True)
			supplier.default_currency = OTHER_CURRENCY
			supplier.save(ignore_permissions=True)
		rows = [row for row in supplier.accounts if row.company == self.company]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].account, self._account(OTHER_CURRENCY))

	def test_unlisted_currency_is_left_alone(self):
		with _configured():
			supplier = self._supplier(CURRENCY)
			foreign_payables.attach_party_account(supplier)
		self.assertEqual(len(supplier.accounts), 0)

	def test_supplier_in_company_currency_gets_no_row(self):
		with _configured(self.company_currency):
			supplier = self._supplier(self.company_currency)
			foreign_payables.attach_party_account(supplier)
		self.assertEqual(len(supplier.accounts), 0)

	# --- settings ---------------------------------------------------------------------------

	def _save_settings(self, currencies):
		settings = frappe.get_doc("Trade Import Settings")
		settings.set("supplier_currencies", [{"currency": c} for c in currencies])
		settings.save(ignore_permissions=True)
		return settings

	def test_settings_save_creates_accounts(self):
		settings = self._save_settings([CURRENCY])
		self.assertIsNotNone(self._account())
		self.assertEqual([row.currency for row in settings.supplier_currencies], [CURRENCY])

	def test_settings_drop_duplicate_currencies(self):
		settings = self._save_settings([CURRENCY, CURRENCY])
		self.assertEqual([row.currency for row in settings.supplier_currencies], [CURRENCY])

	def test_settings_refuse_a_disabled_currency(self):
		frappe.db.set_value("Currency", CURRENCY, "enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			self._save_settings([CURRENCY])

	# --- golden ledger test -----------------------------------------------------------------

	def test_foreign_invoice_paid_from_company_currency_books_the_exchange_difference(self):
		"""Invoice booked at 10, paid from a company-currency account at 10.5: a 50 loss, no stock effect."""
		foreign_payables.ensure_payable_accounts(company=self.company, currencies=[CURRENCY])
		payable = self._account()
		with _configured(CURRENCY):
			supplier = self._supplier(CURRENCY)
			supplier.insert(ignore_permissions=True)
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		if not frappe.db.exists("Item", "_Test FX Service"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "_Test FX Service",
					"item_name": "_Test FX Service",
					"item_group": item_group,
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
		expense = frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"supplier": supplier.name,
				"company": self.company,
				"currency": CURRENCY,
				"conversion_rate": 10.0,
				"posting_date": nowdate(),
				"bill_no": "FX-GOLDEN",
				"bill_date": nowdate(),
				"items": [
					{"item_code": "_Test FX Service", "qty": 1, "rate": 100.0, "expense_account": expense}
				],
			}
		)
		pi.insert(ignore_permissions=True)
		pi.submit()
		self.assertEqual(pi.credit_to, payable)
		self.assertEqual(flt(pi.base_grand_total), 1000.0)

		cash = frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Cash", "is_group": 0}, "name"
		)
		pe = get_payment_entry("Purchase Invoice", pi.name)
		pe.paid_from = cash
		pe.paid_from_account_currency = self.company_currency
		pe.source_exchange_rate = 1
		pe.target_exchange_rate = 10.5
		pe.paid_amount = flt(pe.received_amount * 10.5, 2)
		pe.mode_of_payment = pe.mode_of_payment or "Cash"
		pe.insert(ignore_permissions=True)
		pe.submit()

		def gl(voucher_no):
			return {
				(row.account, flt(row.debit), flt(row.credit))
				for row in frappe.get_all(
					"GL Entry",
					filters={"voucher_no": voucher_no, "is_cancelled": 0},
					fields=["account", "debit", "credit"],
				)
			}

		self.assertIn((payable, 1050.0, 0.0), gl(pe.name))
		self.assertIn((cash, 0.0, 1050.0), gl(pe.name))
		self.assertEqual(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount"), 0)

		exchange_account = frappe.get_cached_value("Company", self.company, "exchange_gain_loss_account")
		journal = frappe.db.sql(
			"select distinct parent from `tabJournal Entry Account` where reference_name=%s",
			pi.name,
			pluck=True,
		)
		self.assertEqual(len(journal), 1)
		self.assertEqual(gl(journal[0]), {(exchange_account, 50.0, 0.0), (payable, 0.0, 50.0)})
