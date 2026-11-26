from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from trade_import import dossier_flow as flow
from trade_import import foreign_payables
from trade_import.tests.test_stock_comptable import _ensure_test_master_data

SAVEPOINT = "test_dossier_flow"
LINKED = ("Purchase Order", "Purchase Order Item", "Purchase Receipt", "Purchase Invoice")


class TestDossierCustomFields(IntegrationTestCase):
	def test_dossier_link_exists_on_purchase_documents(self):
		for doctype in LINKED:
			field = frappe.get_meta(doctype).get_field("dossier_importation")
			self.assertIsNotNone(field, doctype)
			self.assertEqual(field.options, "Dossier Importation", doctype)

	def test_item_has_a_unit_volume(self):
		field = frappe.get_meta("Item").get_field("volume_par_unite")
		self.assertIsNotNone(field)
		self.assertEqual(field.fieldtype, "Float")


class TestDossierDocType(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.savepoint("test_dossier_doctype")
		self.company = _ensure_test_master_data()[0]

	def tearDown(self):
		frappe.db.rollback(save_point="test_dossier_doctype")
		super().tearDown()

	def test_a_dossier_can_be_created_with_a_generated_name(self):
		dossier = frappe.get_doc({"doctype": "Dossier Importation", "company": self.company}).insert()
		self.assertRegex(dossier.name, r"^DI-\d{4}-\d{5}$")
		self.assertEqual(dossier.status, "Brouillon")

	def test_article_table_exists(self):
		meta = frappe.get_meta("Dossier Importation")
		self.assertEqual(meta.get_field("articles").options, "Dossier Importation Article")
		self.assertTrue(frappe.get_meta("Dossier Importation Article").istable)


class TestDossierValidation(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.savepoint("test_dossier_validation")
		self.company, _, _, _, self.item_code, _ = _ensure_test_master_data()
		if not frappe.db.exists("Currency", "EUR"):
			frappe.get_doc({"doctype": "Currency", "currency_name": "EUR", "enabled": 1}).insert(
				ignore_permissions=True
			)

	def tearDown(self):
		frappe.db.rollback(save_point="test_dossier_validation")
		super().tearDown()

	def dossier(self, **row_fields):
		"""A Dossier with one line built by hand: 100 x 2.5 EUR at 10.8, no purchase order."""
		dossier = frappe.get_doc({"doctype": "Dossier Importation", "company": self.company})
		dossier.append(
			"articles",
			{
				"item_code": self.item_code,
				"qty": 100,
				"rate": 2.5,
				"currency": "EUR",
				"exchange_rate": 10.8,
				"conversion_factor": 1,
				**row_fields,
			},
		)
		return dossier.insert()

	def test_line_values_in_the_company_currency(self):
		dossier = self.dossier()
		row = dossier.articles[0]
		self.assertEqual(flt(row.stock_qty), 100)
		self.assertEqual(flt(row.amount), 250)
		self.assertEqual(flt(row.valeur_mad), 2700)
		self.assertEqual(flt(dossier.total_valeur_mad), 2700)

	def test_weight_and_volume_come_from_the_item_card(self):
		frappe.db.set_value(
			"Item", self.item_code, {"weight_per_unit": 1.5, "weight_uom": "Kg", "volume_par_unite": 0.002}
		)
		dossier = self.dossier()
		row = dossier.articles[0]
		self.assertEqual(flt(row.poids_kg), 150)
		self.assertEqual(flt(row.volume_m3, 6), 0.2)
		self.assertEqual(row.source_colisage, flow.FROM_ITEM)
		self.assertEqual(flt(dossier.total_poids_kg), 150)

	def test_values_from_the_packing_list_are_kept(self):
		frappe.db.set_value("Item", self.item_code, {"weight_per_unit": 1.5, "weight_uom": "Kg"})
		dossier = self.dossier(poids_kg=130)
		self.assertEqual(flt(dossier.articles[0].poids_kg), 130)
		self.assertEqual(dossier.articles[0].source_colisage, flow.FROM_LIST)

	def test_status_follows_the_dates(self):
		dossier = frappe.get_doc({"doctype": "Dossier Importation", "company": self.company}).insert()
		self.assertEqual(dossier.status, flow.STATUS_DRAFT)
		dossier = self.dossier()
		self.assertEqual(dossier.status, flow.STATUS_ORDERED)
		dossier.date_depart_reelle = nowdate()
		dossier.save()
		self.assertEqual(dossier.status, flow.STATUS_IN_TRANSIT)
		dossier.date_arrivee_reelle = nowdate()
		dossier.save()
		self.assertEqual(dossier.status, flow.STATUS_IN_CUSTOMS)

	def test_typed_weight_survives_a_resave(self):
		frappe.db.set_value("Item", self.item_code, {"weight_per_unit": 1.5, "weight_uom": "Kg"})
		dossier = self.dossier()
		self.assertEqual(dossier.articles[0].source_colisage, flow.FROM_ITEM)
		dossier.articles[0].poids_kg = 130
		dossier.save()
		self.assertEqual(flt(dossier.articles[0].poids_kg), 130)
		self.assertEqual(dossier.articles[0].source_colisage, flow.FROM_LIST)

	def test_item_card_line_follows_a_quantity_change(self):
		frappe.db.set_value("Item", self.item_code, {"weight_per_unit": 1.5, "weight_uom": "Kg"})
		dossier = self.dossier()
		dossier.articles[0].qty = 200
		dossier.save()
		self.assertEqual(flt(dossier.articles[0].poids_kg), 300)
		self.assertEqual(dossier.articles[0].source_colisage, flow.FROM_ITEM)

	def test_a_missing_exchange_rate_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "taux de change"):
			self.dossier(exchange_rate=0)

	def test_company_currency_line_ignores_the_stored_rate(self):
		currency = frappe.get_cached_value("Company", self.company, "default_currency")
		row = self.dossier(currency=currency, exchange_rate=7).articles[0]
		self.assertEqual(flt(row.exchange_rate), 1)
		self.assertEqual(flt(row.valeur_mad), flt(row.amount))


def make_supplier(name, currency):
	if not frappe.db.exists("Supplier", name):
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": name,
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
				or frappe.get_doc({"doctype": "Supplier Group", "supplier_group_name": "_Test DI Group"})
				.insert(ignore_permissions=True)
				.name,
				"default_currency": currency,
			}
		).insert(ignore_permissions=True)
	return name


def make_order(supplier, currency, rate, qty, price, item_code, warehouse, company):
	for code in {currency}:
		if not frappe.db.exists("Currency", code):
			frappe.get_doc({"doctype": "Currency", "currency_name": code, "enabled": 1}).insert(
				ignore_permissions=True
			)
	order = frappe.get_doc(
		{
			"doctype": "Purchase Order",
			"supplier": supplier,
			"company": company,
			"currency": currency,
			"conversion_rate": rate,
			"schedule_date": add_days(nowdate(), 10),
			"items": [
				{
					"item_code": item_code,
					"qty": qty,
					"rate": price,
					"warehouse": warehouse,
					"schedule_date": add_days(nowdate(), 10),
				}
			],
		}
	).insert()
	order.submit()
	return order


class DossierTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.savepoint(SAVEPOINT)
		patcher = patch.object(foreign_payables, "get_configured_currencies", return_value=[])
		patcher.start()
		self.addCleanup(patcher.stop)
		(
			self.company,
			self.company_currency,
			_,
			self.warehouse,
			self.item_code,
			_,
		) = _ensure_test_master_data()
		self.supplier_a = make_supplier("_Test DI Supplier A", "EUR")
		self.supplier_b = make_supplier("_Test DI Supplier B", "USD")

	def tearDown(self):
		frappe.db.rollback(save_point=SAVEPOINT)
		super().tearDown()

	def order(self, supplier="_Test DI Supplier A", currency="EUR", rate=10.8, qty=100, price=2.5):
		return make_order(supplier, currency, rate, qty, price, self.item_code, self.warehouse, self.company)

	def dossier(self, **fields):
		return frappe.get_doc({"doctype": "Dossier Importation", "company": self.company, **fields}).insert()


class TestOrderLines(DossierTestCase):
	def test_an_open_line_is_listed_with_its_order_data(self):
		order = self.order(qty=100)
		lines = [l for l in flow.get_open_order_lines(self.company) if l["purchase_order"] == order.name]
		self.assertEqual(len(lines), 1)
		line = lines[0]
		self.assertEqual(
			(
				line["supplier"],
				line["item_code"],
				flt(line["qty"]),
				line["currency"],
				flt(line["conversion_rate"]),
			),
			(self.supplier_a, self.item_code, 100, "EUR", 10.8),
		)

	def test_the_supplier_filter(self):
		a, b = self.order(), self.order("_Test DI Supplier B", "USD", 9.9)
		names = {l["purchase_order"] for l in flow.get_open_order_lines(self.company, self.supplier_b)}
		self.assertEqual(names & {a.name, b.name}, {b.name})

	def test_a_line_taken_by_a_dossier_is_no_longer_listed(self):
		order = self.order()
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [order.items[0].name])
		listed = {l["purchase_order"] for l in flow.get_open_order_lines(self.company)}
		self.assertNotIn(order.name, listed)

	def test_adding_a_line_links_the_order_to_the_dossier(self):
		order = self.order()
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [order.items[0].name])
		self.assertEqual(
			frappe.db.get_value("Purchase Order", order.name, "dossier_importation"), dossier.name
		)
		self.assertEqual(
			frappe.db.get_value("Purchase Order Item", order.items[0].name, "dossier_importation"),
			dossier.name,
		)

	def test_a_lowered_line_leaves_the_rest_for_another_dossier(self):
		order = self.order(qty=100)
		first = self.dossier()
		flow.add_order_lines(first.name, [order.items[0].name])
		first.reload()
		first.articles[0].qty = 60  # only 60 shipped
		first.save()
		available = flow.available_qty([order.items[0].name])
		self.assertEqual(flt(available[order.items[0].name]), 40)
		second = self.dossier()
		flow.add_order_lines(second.name, [order.items[0].name])
		second.reload()
		self.assertEqual(flt(second.articles[0].qty), 40)

	def test_a_line_cannot_be_over_allocated(self):
		order = self.order(qty=100)
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [order.items[0].name])
		dossier.reload()
		dossier.articles[0].qty = 150
		with self.assertRaisesRegex(frappe.ValidationError, "dépasse"):
			dossier.save()

	def test_every_line_must_belong_to_the_dossier_company(self):
		order = self.order()
		other = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "_Test DI Other",
				"abbr": "TDO",
				"default_currency": self.company_currency,
				"country": frappe.db.get_value("Company", self.company, "country"),
			}
		).insert(ignore_permissions=True)
		dossier = frappe.get_doc({"doctype": "Dossier Importation", "company": other.name}).insert()
		with self.assertRaisesRegex(frappe.ValidationError, "n'est plus disponible"):
			flow.add_order_lines(dossier.name, [order.items[0].name])


class TestOrderLineGuards(DossierTestCase):
	def other_company_dossier(self):
		other = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "_Test DI Other",
				"abbr": "TDO",
				"default_currency": self.company_currency,
				"country": frappe.db.get_value("Company", self.company, "country"),
			}
		).insert(ignore_permissions=True)
		return frappe.get_doc({"doctype": "Dossier Importation", "company": other.name}).insert()

	def row_for(self, order, **overrides):
		line = order.items[0]
		row = {
			"purchase_order": order.name,
			"purchase_order_item": line.name,
			"supplier": order.supplier,
			"item_code": line.item_code,
			"qty": line.qty,
			"uom": line.uom,
			"conversion_factor": line.conversion_factor,
			"currency": order.currency,
			"rate": line.rate,
			"exchange_rate": order.conversion_rate,
		}
		return {**row, **overrides}

	def test_a_hand_added_row_of_another_company_is_refused(self):
		order = self.order()
		dossier = self.other_company_dossier()
		dossier.append("articles", self.row_for(order))
		with self.assertRaisesRegex(frappe.ValidationError, "n'appartient pas"):
			dossier.save()

	def test_a_row_naming_the_wrong_order_is_refused(self):
		order, other_order = self.order(), self.order()
		dossier = self.dossier()
		dossier.append("articles", self.row_for(order, purchase_order=other_order.name))
		with self.assertRaisesRegex(frappe.ValidationError, "appartient à la commande"):
			dossier.save()

	def test_a_row_of_a_draft_order_is_refused(self):
		order = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": self.supplier_a,
				"company": self.company,
				"currency": "EUR",
				"conversion_rate": 10.8,
				"schedule_date": add_days(nowdate(), 10),
				"items": [
					{
						"item_code": self.item_code,
						"qty": 10,
						"rate": 2.5,
						"warehouse": self.warehouse,
						"schedule_date": add_days(nowdate(), 10),
					}
				],
			}
		).insert()
		dossier = self.dossier()
		dossier.append("articles", self.row_for(order))
		with self.assertRaisesRegex(frappe.ValidationError, "n'est pas validée"):
			dossier.save()

	def test_a_missing_order_line_is_refused(self):
		order = self.order()
		dossier = self.dossier()
		dossier.append("articles", self.row_for(order, purchase_order_item="does-not-exist"))
		with self.assertRaises(frappe.ValidationError):
			dossier.save()


class TestReceivedOutsideDossiers(DossierTestCase):
	def receive(self, order, qty):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		receipt = make_purchase_receipt(order.name)
		receipt.items[0].qty = qty
		receipt.insert()
		receipt.submit()
		return receipt

	def listed(self, order):
		return [l for l in flow.get_open_order_lines(self.company) if l["purchase_order"] == order.name]

	def test_a_plain_receipt_reduces_what_is_available(self):
		order = self.order(qty=100)
		line = order.items[0].name
		self.receive(order, 30)
		self.assertEqual(flt(flow.available_qty([line])[line]), 70)
		listed = self.listed(order)
		self.assertEqual(len(listed), 1)
		self.assertEqual(flt(listed[0]["qty"]), 70)

	def test_a_fully_received_line_is_not_listed(self):
		order = self.order(qty=100)
		self.receive(order, 100)
		self.assertEqual(self.listed(order), [])

	def test_closed_and_on_hold_orders_are_not_listed(self):
		for status in ("Closed", "On Hold"):
			order = self.order()
			self.assertEqual(len(self.listed(order)), 1)
			frappe.db.set_value("Purchase Order", order.name, "status", status)
			self.assertEqual(self.listed(order), [], status)

	def test_received_outside_then_a_dossier_takes_the_rest(self):
		order = self.order(qty=100)
		line = order.items[0].name
		self.receive(order, 30)
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [line])
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].qty), 70)
		self.assertEqual(flt(flow.available_qty([line])[line]), 0)
		dossier.articles[0].qty = 50
		dossier.save()
		self.assertEqual(flt(flow.available_qty([line])[line]), 20)
