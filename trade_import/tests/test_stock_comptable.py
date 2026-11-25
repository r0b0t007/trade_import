import frappe
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, getdate, nowdate

from trade_import.stock_comptable import get_stock_comptable_qty
from trade_import.trade_import.report.stock_vs_stock_comptable.stock_vs_stock_comptable import (
	execute as run_stock_comparison_report,
)


def _ensure_test_master_data() -> tuple[str, str, str, str, str, str]:
	"""Create isolated test Company, Currency, Price List, Warehouse, Item, and Customer if absent."""
	company = "_Test SC Company"
	abbr = "TSC"
	today = getdate(nowdate())
	year = str(today.year)

	if not frappe.db.exists("Party Type", "Customer"):
		frappe.get_doc(
			{
				"doctype": "Party Type",
				"party_type": "Customer",
				"account_type": "Receivable",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)

	if not frappe.db.exists("Stock Entry Type", "Material Receipt"):
		frappe.get_doc(
			{
				"doctype": "Stock Entry Type",
				"name": "Material Receipt",
				"purpose": "Material Receipt",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists(
		"Fiscal Year", {"year_start_date": ("<=", today), "year_end_date": (">=", today)}
	):
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"_Test SC FY {year}",
				"year_start_date": f"{year}-01-01",
				"year_end_date": f"{year}-12-31",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Company", company):
		country = frappe.db.get_value("Country", {"name": "Morocco"}, "name") or frappe.db.get_value(
			"Country", {}, "name"
		)
		currency = frappe.db.get_value("Currency", {"name": "MAD"}, "name") or frappe.db.get_value(
			"Currency", {"enabled": 1}, "name"
		)
		frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": company,
				"abbr": abbr,
				"default_currency": currency,
				"country": country,
				"enable_perpetual_inventory": 0,
			}
		).insert(ignore_permissions=True)
	else:
		currency = frappe.db.get_value("Company", company, "default_currency")

	price_list = "_Test SC Selling"
	if not frappe.db.exists("Price List", price_list):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": price_list,
				"enabled": 1,
				"selling": 1,
				"buying": 0,
				"currency": currency,
			}
		).insert(ignore_permissions=True)

	warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
	if not warehouse:
		parent = frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")
		wh = frappe.get_doc(
			{
				"doctype": "Warehouse",
				"warehouse_name": "_Test SC Store",
				"company": company,
				"parent_warehouse": parent,
			}
		).insert(ignore_permissions=True)
		warehouse = wh.name

	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	if not item_group:
		ig = frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": "_Test SC Item Group",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		item_group = ig.name

	uom = frappe.db.get_value("UOM", {"enabled": 1}, "name")
	if not uom:
		u = frappe.get_doc({"doctype": "UOM", "uom_name": "Nos", "enabled": 1}).insert(
			ignore_permissions=True
		)
		uom = u.name

	item_code = "_Test SC Item"
	if not frappe.db.exists("Item", item_code):
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "_Test SC Item",
				"item_group": item_group,
				"stock_uom": uom,
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)

	cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	if not cg:
		cg_doc = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": "_Test SC Customer Group",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		cg = cg_doc.name

	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	if not territory:
		t_doc = frappe.get_doc(
			{
				"doctype": "Territory",
				"territory_name": "_Test SC Territory",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		territory = t_doc.name

	customer = "_Test SC Customer"
	if not frappe.db.exists("Customer", customer):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer,
				"customer_type": "Company",
				"customer_group": cg,
				"territory": territory,
			}
		).insert(ignore_permissions=True)

	return company, currency, price_list, warehouse, item_code, customer


def _get_real_stock_qty(item_code: str, warehouse: str) -> float:
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


class TestStockComptable(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		(
			self.company,
			self.currency,
			self.price_list,
			self.warehouse,
			self.item_code,
			self.customer,
		) = _ensure_test_master_data()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _receive_stock(self, qty: float, rate: float = 50.0):
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": self.company,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"t_warehouse": self.warehouse,
						"qty": qty,
						"basic_rate": rate,
					}
				],
			}
		).insert()
		se.submit()
		return se

	def _make_sales_order(self, qty: float, rate: float = 100.0):
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": self.company,
				"currency": self.currency,
				"selling_price_list": self.price_list,
				"customer": self.customer,
				"transaction_date": nowdate(),
				"delivery_date": nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.warehouse,
						"qty": qty,
						"rate": rate,
					}
				],
			}
		).insert()
		so.submit()
		return so

	def test_disabled_by_default_does_not_post(self):
		with self.change_settings(
			"Trade Import Settings",
			{"enable_stock_comptable": 0, "require_delivery_note_before_invoice": 0},
		):
			self._receive_stock(10)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 0.0)
			self.assertEqual(
				frappe.db.count("Stock Comptable Entry", {"item_code": self.item_code}),
				0,
			)

	def test_bl_moves_stock_only_and_invoice_moves_stock_comptable(self):
		with self.change_settings(
			"Trade Import Settings",
			{"enable_stock_comptable": 1, "require_delivery_note_before_invoice": 1},
		):
			self._receive_stock(10)
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 10.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 10.0)

			so = self._make_sales_order(4)
			dn = make_delivery_note(so.name)
			dn.insert()
			dn.submit()

			# Delivery Note reduces physical stock only; Stock Comptable stays at 10.
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 6.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 10.0)

			# Report shows the 4-unit difference (delivered, not yet invoiced).
			_, rows, *_ = run_stock_comparison_report(
				{"company": self.company, "item_code": self.item_code, "only_differences": 1}
			)
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0].difference, 4.0)

			# Converting BL to Sales Invoice deducts Stock Comptable only.
			si = make_sales_invoice(dn.name)
			si.insert()
			si.submit()

			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 6.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 6.0)

			_, diff_rows, *_ = run_stock_comparison_report(
				{"company": self.company, "item_code": self.item_code, "only_differences": 1}
			)
			self.assertEqual(diff_rows, [])

			# Cancelling the Sales Invoice restores Stock Comptable to 10 and keeps Stock at 6.
			si.cancel()
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 6.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 10.0)

			# Cancelling the Delivery Note afterwards succeeds and restores Stock to 10.
			dn.reload()
			dn.cancel()
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 10.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 10.0)

	def test_grouped_bls_into_single_invoice(self):
		with (
			self.change_settings(
				"Trade Import Settings",
				{"enable_stock_comptable": 1, "require_delivery_note_before_invoice": 1},
			),
			self.change_settings("Selling Settings", {"allow_multiple_items": 1}),
		):
			self._receive_stock(12)
			so = self._make_sales_order(10)

			dn1 = make_delivery_note(so.name)
			dn1.items[0].qty = 4
			dn1.insert()
			dn1.submit()

			dn2 = make_delivery_note(so.name)
			dn2.items[0].qty = 6
			dn2.insert()
			dn2.submit()

			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 2.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 12.0)

			si = make_sales_invoice(dn1.name)
			si_from_dn2 = make_sales_invoice(dn2.name)
			for row in si_from_dn2.items:
				si.append("items", row.as_dict())
			si.insert()
			si.submit()

			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 2.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 2.0)

	def test_requires_delivery_note_before_invoice(self):
		with self.change_settings(
			"Trade Import Settings",
			{"enable_stock_comptable": 1, "require_delivery_note_before_invoice": 1},
		):
			self._receive_stock(5)

			direct_si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": self.company,
					"currency": self.currency,
					"selling_price_list": self.price_list,
					"customer": self.customer,
					"posting_date": nowdate(),
					"items": [
						{
							"item_code": self.item_code,
							"warehouse": self.warehouse,
							"qty": 2,
							"rate": 100,
						}
					],
				}
			)
			self.assertRaises(frappe.ValidationError, direct_si.insert)

			direct_with_stock = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": self.company,
					"currency": self.currency,
					"selling_price_list": self.price_list,
					"customer": self.customer,
					"posting_date": nowdate(),
					"update_stock": 1,
					"items": [
						{
							"item_code": self.item_code,
							"warehouse": self.warehouse,
							"qty": 2,
							"rate": 100,
						}
					],
				}
			)
			self.assertRaises(frappe.ValidationError, direct_with_stock.insert)

	def test_credit_note_restores_stock_comptable_only(self):
		with self.change_settings(
			"Trade Import Settings",
			{"enable_stock_comptable": 1, "require_delivery_note_before_invoice": 1},
		):
			self._receive_stock(10)
			so = self._make_sales_order(5)
			dn = make_delivery_note(so.name)
			dn.insert()
			dn.submit()
			si = make_sales_invoice(dn.name)
			si.insert()
			si.submit()

			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 5.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 5.0)

			credit_note = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": self.company,
					"currency": self.currency,
					"selling_price_list": self.price_list,
					"customer": self.customer,
					"posting_date": nowdate(),
					"is_return": 1,
					"return_against": si.name,
					"items": [
						{
							"item_code": self.item_code,
							"warehouse": self.warehouse,
							"qty": -2,
							"rate": 100,
							"sales_invoice_item": si.items[0].name,
							"delivery_note": dn.name,
							"dn_detail": dn.items[0].name,
						}
					],
				}
			).insert()
			credit_note.submit()

			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 5.0)
			self.assertEqual(get_stock_comptable_qty(self.item_code, self.warehouse), 7.0)

	def test_negative_stock_comptable_blocked_when_disallowed(self):
		with self.change_settings(
			"Trade Import Settings",
			{
				"enable_stock_comptable": 1,
				"require_delivery_note_before_invoice": 0,
				"allow_negative_stock_comptable": 0,
			},
		):
			si = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": self.company,
					"currency": self.currency,
					"selling_price_list": self.price_list,
					"customer": self.customer,
					"posting_date": nowdate(),
					"items": [
						{
							"item_code": self.item_code,
							"warehouse": self.warehouse,
							"qty": 3,
							"rate": 100,
						}
					],
				}
			).insert()
			self.assertRaises(frappe.ValidationError, si.submit)
