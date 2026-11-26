import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate

from trade_import import dossier_flow as flow
from trade_import.tests.test_stock_comptable import _ensure_test_master_data

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
