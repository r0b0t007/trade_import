import frappe
from frappe.tests import IntegrationTestCase

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
