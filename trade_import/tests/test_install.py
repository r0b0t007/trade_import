import frappe
from frappe.tests import IntegrationTestCase


class TestInstall(IntegrationTestCase):
	def test_app_is_installed(self):
		self.assertIn("trade_import", frappe.get_installed_apps())

	def test_required_apps_are_installed(self):
		installed = frappe.get_installed_apps()
		for app in ["erpnext"]:
			self.assertIn(app, installed)
