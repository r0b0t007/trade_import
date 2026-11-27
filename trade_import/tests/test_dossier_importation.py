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

	def test_dossier_links_are_indexed(self):
		for doctype in LINKED:
			self.assertTrue(frappe.get_meta(doctype).get_field("dossier_importation").search_index, doctype)

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


def make_order(supplier, currency, rate, qty, price, item_code, warehouse, company, **header):
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
			**header,
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

	def order(self, supplier="_Test DI Supplier A", currency="EUR", rate=10.8, qty=100, price=2.5, **header):
		return make_order(
			supplier, currency, rate, qty, price, self.item_code, self.warehouse, self.company, **header
		)

	def dossier(self, **fields):
		return frappe.get_doc({"doctype": "Dossier Importation", "company": self.company, **fields}).insert()


class TestDossierValidation(DossierTestCase):
	def setUp(self):
		super().setUp()
		self.order_doc = self.order(qty=1000, price=2.5)

	def dossier(self, **row_fields):
		"""A Dossier with one line built by hand: 100 x 2.5 EUR at 10.8, on a real order line."""
		line = self.order_doc.items[0]
		dossier = frappe.get_doc({"doctype": "Dossier Importation", "company": self.company})
		dossier.append(
			"articles",
			{
				"purchase_order": self.order_doc.name,
				"purchase_order_item": line.name,
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

	def test_a_row_without_an_order_line_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "ligne de commande est requise"):
			self.dossier(purchase_order=None, purchase_order_item=None)

	def test_a_zero_or_negative_quantity_is_refused(self):
		for qty in (0, -5):
			with self.assertRaisesRegex(frappe.ValidationError, "quantité doit être positive"):
				self.dossier(qty=qty)

	def test_a_quantity_below_the_received_quantity_is_refused(self):
		dossier = self.dossier(qty=100)
		frappe.db.set_value("Dossier Importation Article", dossier.articles[0].name, "received_qty", 50)
		dossier.reload()
		dossier.articles[0].qty = 30
		with self.assertRaisesRegex(frappe.ValidationError, "inférieure à la quantité reçue"):
			dossier.save()

	def test_a_received_quantity_above_the_line_counts_in_full(self):
		dossier = self.dossier(qty=100)
		row = dossier.articles[0]
		frappe.db.set_value(
			"Dossier Importation Article", row.name, {"qty": 30, "received_qty": 50}
		)  # over-receipt, skips validate
		line = row.purchase_order_item
		# order 1000: 50 taken (the received quantity outweighs the 30)
		self.assertEqual(flt(flow.available_qty([line])[line]), 950)

	def test_the_value_follows_the_net_price_of_the_order(self):
		order = self.order(qty=100, price=2.5, additional_discount_percentage=10)
		line = order.items[0]
		self.assertEqual(flt(line.net_rate, 4), 2.25)
		dossier = frappe.get_doc({"doctype": "Dossier Importation", "company": self.company}).insert()
		flow.add_order_lines(dossier.name, [line.name])
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].rate, 4), 2.25)
		self.assertEqual(flt(dossier.articles[0].valeur_mad), 2430)


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

	def test_a_lowered_line_added_again_raises_the_same_row(self):
		order = self.order(qty=100)
		line = order.items[0].name
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [line])
		dossier.reload()
		dossier.articles[0].qty = 60
		dossier.save()
		flow.add_order_lines(dossier.name, [line])
		dossier.reload()
		self.assertEqual(len(dossier.articles), 1)
		self.assertEqual(flt(dossier.articles[0].qty), 100)

	def test_the_same_name_twice_adds_one_row(self):
		order = self.order()
		line = order.items[0].name
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [line, line])
		dossier.reload()
		self.assertEqual(len(dossier.articles), 1)
		self.assertEqual(flt(dossier.articles[0].qty), 100)

	def test_non_stock_items_are_not_listed(self):
		item_group, uom = frappe.db.get_value("Item", self.item_code, ["item_group", "stock_uom"])
		if not frappe.db.exists("Item", "_Test DI Service"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "_Test DI Service",
					"item_group": item_group,
					"stock_uom": uom,
					"is_stock_item": 0,
				}
			).insert(ignore_permissions=True)
		service = make_order(
			self.supplier_a, "EUR", 10.8, 10, 2.5, "_Test DI Service", self.warehouse, self.company
		)
		stock = self.order()
		names = {l["purchase_order"] for l in flow.get_open_order_lines(self.company)}
		self.assertIn(stock.name, names)
		self.assertNotIn(service.name, names)

	def test_drop_ship_lines_and_subcontracted_orders_are_not_listed(self):
		drop_ship, subcontracted, normal = self.order(), self.order(), self.order()
		frappe.db.set_value("Purchase Order Item", drop_ship.items[0].name, "delivered_by_supplier", 1)
		frappe.db.set_value("Purchase Order", subcontracted.name, "is_subcontracted", 1)
		names = {l["purchase_order"] for l in flow.get_open_order_lines(self.company)}
		self.assertIn(normal.name, names)
		self.assertNotIn(drop_ship.name, names)
		self.assertNotIn(subcontracted.name, names)

	def test_a_user_only_sees_the_orders_they_may_read(self):
		a, b = self.order(), self.order("_Test DI Supplier B", "USD", 9.9)
		email = "_test_di_user@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Test DI",
					"send_welcome_email": 0,
					"roles": [{"role": "Purchase User"}],
				}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "User Permission", "user": email, "allow": "Supplier", "for_value": self.supplier_a}
		).insert(ignore_permissions=True)
		admin = frappe.session.user
		frappe.set_user(email)
		try:
			names = {l["purchase_order"] for l in flow.get_open_order_lines(self.company)}
		finally:
			frappe.set_user(admin)
		self.assertIn(a.name, names)
		self.assertNotIn(b.name, names)

	def test_a_line_cannot_be_over_allocated(self):
		order = self.order(qty=100)
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [order.items[0].name])
		dossier.reload()
		dossier.articles[0].qty = 150
		with self.assertRaisesRegex(frappe.ValidationError, "dépasse") as ctx:
			dossier.save()
		self.assertIn(order.name, str(ctx.exception))
		self.assertIn(self.item_code, str(ctx.exception))
		self.assertNotIn(order.items[0].name, str(ctx.exception))

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
		with self.assertRaisesRegex(frappe.ValidationError, "n'existe pas"):
			dossier.save()

	def test_the_same_order_line_on_two_rows_is_refused(self):
		order = self.order()
		dossier = self.dossier()
		dossier.append("articles", self.row_for(order, qty=50))
		dossier.append("articles", self.row_for(order, qty=50))
		with self.assertRaisesRegex(frappe.ValidationError, "figure sur les lignes 1 et 2"):
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
		for status in ("Closed", "On Hold", "Delivered"):
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


class TestReceipts(DossierTestCase):
	def loaded(self, *orders):
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [o.items[0].name for o in orders])
		dossier.reload()
		return dossier

	def test_one_draft_receipt_per_order(self):
		a1, a2 = self.order(), self.order(qty=40, rate=10.9)
		b = self.order("_Test DI Supplier B", "USD", 9.9)
		dossier = self.loaded(a1, a2, b)
		names = flow.create_receipts(dossier.name)
		self.assertEqual(len(names), 3)
		by_order = {}
		for name in names:
			receipt = frappe.get_doc("Purchase Receipt", name)
			self.assertEqual(receipt.docstatus, 0)
			self.assertEqual(receipt.dossier_importation, dossier.name)
			self.assertEqual(len(receipt.items), 1)
			by_order[receipt.items[0].purchase_order] = receipt
		self.assertEqual(set(by_order), {a1.name, a2.name, b.name})
		for order, qty, rate in ((a1, 100, 10.8), (a2, 40, 10.9), (b, 100, 9.9)):
			receipt = by_order[order.name]
			self.assertEqual(receipt.items[0].purchase_order_item, order.items[0].name)
			self.assertEqual(flt(receipt.items[0].qty), qty)
			self.assertEqual(flt(receipt.conversion_rate), rate)

	def test_each_receipt_keeps_the_rate_and_discount_of_its_own_order(self):
		first = self.order(qty=100, rate=10.8, additional_discount_percentage=5)
		second = self.order(qty=40, rate=10.9)
		dossier = self.loaded(first, second)
		receipts = {
			r.items[0].purchase_order: r
			for r in (frappe.get_doc("Purchase Receipt", n) for n in flow.create_receipts(dossier.name))
		}
		self.assertEqual(flt(receipts[first.name].items[0].base_net_amount, 2), 2565.00)
		self.assertEqual(flt(receipts[second.name].items[0].base_net_amount, 2), 1090.00)

	def test_the_receipt_takes_the_quantity_of_the_dossier_line(self):
		order = self.order(qty=100)
		dossier = self.loaded(order)
		dossier.articles[0].qty = 60
		dossier.save()
		receipt = frappe.get_doc("Purchase Receipt", flow.create_receipts(dossier.name)[0])
		self.assertEqual(flt(receipt.items[0].qty), 60)

	def test_a_second_draft_receipt_is_refused(self):
		dossier = self.loaded(self.order())
		flow.create_receipts(dossier.name)
		with self.assertRaisesRegex(frappe.ValidationError, "brouillon"):
			flow.create_receipts(dossier.name)

	def test_nothing_to_receive_is_refused(self):
		dossier = self.dossier()
		with self.assertRaisesRegex(frappe.ValidationError, "Rien à réceptionner"):
			flow.create_receipts(dossier.name)

	def test_submitting_the_receipt_updates_the_dossier(self):
		order = self.order(qty=100, rate=10.8)
		dossier = self.loaded(order)
		receipt = frappe.get_doc("Purchase Receipt", flow.create_receipts(dossier.name)[0])
		receipt.conversion_rate = 11.0  # the receipt's own rate replaces the order's
		receipt.submit()
		dossier.reload()
		row = dossier.articles[0]
		self.assertEqual(flt(row.received_qty), 100)
		self.assertEqual(row.purchase_receipt, receipt.name)
		self.assertEqual(flt(row.exchange_rate), 11.0)
		self.assertEqual(flt(row.valeur_mad), 2750)
		self.assertEqual(dossier.status, flow.STATUS_RECEIVED)

	def test_a_partial_receipt_keeps_the_dossier_open(self):
		order = self.order(qty=100)
		dossier = self.loaded(order)
		receipt = frappe.get_doc("Purchase Receipt", flow.create_receipts(dossier.name)[0])
		receipt.items[0].qty = 70
		receipt.items[0].received_qty = 70
		receipt.save()
		receipt.submit()
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 70)
		self.assertNotEqual(dossier.status, flow.STATUS_RECEIVED)
		second = frappe.get_doc("Purchase Receipt", flow.create_receipts(dossier.name)[0])
		self.assertEqual(flt(second.items[0].qty), 30)
		second.submit()
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 100)
		self.assertEqual(dossier.status, flow.STATUS_RECEIVED)

	def test_cancelling_the_receipt_reopens_the_dossier(self):
		order = self.order(qty=100, rate=10.8)
		dossier = self.loaded(order)
		receipt = frappe.get_doc("Purchase Receipt", flow.create_receipts(dossier.name)[0])
		receipt.conversion_rate = 11.0
		receipt.submit()
		receipt.cancel()
		dossier.reload()
		row = dossier.articles[0]
		self.assertEqual(flt(row.received_qty), 0)
		self.assertEqual(flt(row.exchange_rate), 10.8)  # back to the order's rate
		self.assertFalse(row.purchase_receipt)
		self.assertNotEqual(dossier.status, flow.STATUS_RECEIVED)

	def submitted_receipt(self, dossier, qty=None):
		receipt = frappe.get_doc("Purchase Receipt", flow.create_receipts(dossier.name)[0])
		if qty is not None:
			receipt.items[0].qty = qty
			receipt.items[0].received_qty = qty
			receipt.save()
		receipt.submit()
		return receipt

	def test_a_receipt_can_be_cancelled_when_the_line_is_over_allocated_but_users_stay_guarded(self):
		order = self.order(qty=100)
		dossier = self.loaded(order)
		receipt = self.submitted_receipt(dossier)
		row = dossier.articles[0]
		frappe.db.set_value("Dossier Importation Article", row.name, "qty", 500)  # skips validate
		receipt.cancel()
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 0)
		self.assertEqual(flt(dossier.articles[0].qty), 500)
		with self.assertRaisesRegex(frappe.ValidationError, "dépasse"):
			dossier.save()

	def test_cancelling_one_of_two_receipts_falls_back_to_the_earlier_one(self):
		order = self.order(qty=100)
		dossier = self.loaded(order)
		first = self.submitted_receipt(dossier, qty=60)
		second = self.submitted_receipt(dossier)
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 100)
		self.assertEqual(dossier.articles[0].purchase_receipt, second.name)
		second.cancel()
		dossier.reload()
		self.assertEqual(dossier.articles[0].purchase_receipt, first.name)
		self.assertEqual(flt(dossier.articles[0].received_qty), 60)

	def test_drafts_and_receipts_of_another_dossier_do_not_count(self):
		order = self.order(qty=100)
		dossier = self.loaded(order)
		dossier.articles[0].qty = 60
		dossier.save()
		other = self.loaded(order)  # takes the remaining 40 of the same order line
		self.assertEqual(flt(other.articles[0].qty), 40)
		flow.create_receipts(dossier.name)  # a draft
		flow.refresh_from_receipts(dossier.name)
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 0)
		self.submitted_receipt(other)
		flow.refresh_from_receipts(dossier.name)
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 0)
		self.assertFalse(dossier.articles[0].purchase_receipt)
		other.reload()
		self.assertEqual(flt(other.articles[0].received_qty), 40)

	def plain_receipt(self, order, qty=None):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		receipt = make_purchase_receipt(order.name)
		if qty is not None:
			receipt.items[0].qty = qty
		return receipt.insert()

	def test_a_plain_receipt_for_a_dossier_line_gets_the_dossier_link(self):
		order = self.order(qty=100)
		dossier = self.loaded(order)
		receipt = self.plain_receipt(order)
		self.assertEqual(receipt.dossier_importation, dossier.name)
		receipt.submit()
		dossier.reload()
		self.assertEqual(flt(dossier.articles[0].received_qty), 100)
		self.assertEqual(dossier.status, flow.STATUS_RECEIVED)
		dossier.save()

	def test_a_plain_receipt_for_an_untouched_order_stays_free(self):
		receipt = self.plain_receipt(self.order())
		self.assertFalse(receipt.dossier_importation)

	def test_a_plain_receipt_for_lines_held_by_two_dossiers_is_refused(self):
		order = self.order(qty=100)
		first = self.loaded(order)
		first.articles[0].qty = 60
		first.save()
		self.loaded(order)
		with self.assertRaisesRegex(frappe.ValidationError, "depuis le dossier"):
			self.plain_receipt(order)

	def test_a_receipt_naming_another_dossier_is_refused(self):
		held = self.order()
		dossier, other = self.loaded(held), self.dossier()
		receipt = self.plain_receipt(held)
		self.assertEqual(receipt.dossier_importation, dossier.name)
		receipt.dossier_importation = other.name
		with self.assertRaisesRegex(frappe.ValidationError, "depuis le dossier"):
			receipt.save()

	# Cancelling after the order is Closed is not tested here: ERPNext refuses it before our hook runs.
