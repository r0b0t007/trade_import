from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, nowdate, nowtime

from trade_import import stock_comptable
from trade_import.patches import add_unique_stock_comptable_bin as unique_bin_patch
from trade_import.stock_comptable import get_stock_comptable_qty
from trade_import.tests.test_stock_comptable import _ensure_test_master_data, _get_real_stock_qty
from trade_import.trade_import.doctype.trade_import_settings.trade_import_settings import (
	TradeImportSettings,
)

ON = {
	"enable_stock_comptable": 1,
	"require_delivery_note_before_invoice": 0,
	"allow_negative_stock_comptable": 0,
}
OFF = {"enable_stock_comptable": 0}
SETTINGS = "Trade Import Settings"
# Kept before any patching: the toggle handler is replaced in setUp so that switching the
# setting in a test never seeds the whole site, whatever data the site holds.
REAL_TOGGLE_HANDLER = TradeImportSettings._handle_stock_comptable_toggle


def _stub_sle(item_code, qty, qty_after, detail="d1"):
	return {
		"name": f"sle-{item_code}-{qty}",
		"item_code": item_code,
		"warehouse": "W",
		"stock_uom": "Nos",
		"voucher_detail_no": detail,
		"actual_qty": qty,
		"qty_after_transaction": qty_after,
		"posting_datetime": None,
		"creation": None,
	}


class TestStockComptableIntegrity(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.enterContext(
			patch.object(TradeImportSettings, "_handle_stock_comptable_toggle", lambda self: None)
		)
		(
			self.company,
			self.currency,
			self.price_list,
			self.warehouse,
			self.item_code,
			self.customer,
		) = _ensure_test_master_data()

	def tearDown(self):
		frappe.set_user("Administrator")
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

	def _reconcile(self, qty: float):
		sr = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"purpose": "Stock Reconciliation",
				"company": self.company,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.warehouse,
						"qty": qty,
						"valuation_rate": 50.0,
					}
				],
			}
		).insert()
		sr.submit()
		return sr

	def _comptable(self) -> float:
		return get_stock_comptable_qty(self.item_code, self.warehouse)

	def _user_with_role(self, role: str) -> str:
		email = f"sc-{role.lower().replace(' ', '-')}@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "SC",
					"send_welcome_email": 0,
					"roles": [{"role": role}],
				}
			).insert(ignore_permissions=True)
		return email

	# --- Stock Reconciliation ---------------------------------------------------------------

	def test_adjustment_entry_reconciliation_never_moves_comptable(self):
		doc = frappe._dict(
			doctype="Stock Reconciliation", name="SR-X", purpose="Adjustment Entry", company=self.company
		)
		with (
			self.change_settings(SETTINGS, ON),
			patch.object(stock_comptable, "_post_movements") as post,
			patch.object(stock_comptable, "_stock_ledger_entries_of") as entries,
		):
			stock_comptable.on_submit_stock_reconciliation(doc)
		post.assert_not_called()
		entries.assert_not_called()

	def test_reconciliation_moves_comptable_by_the_counted_delta(self):
		with self.change_settings(SETTINGS, ON):
			self._receive_stock(10)
			self._reconcile(7)
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 7.0)
			self.assertEqual(self._comptable(), 7.0)
			self._reconcile(12)
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 12.0)
			self.assertEqual(self._comptable(), 12.0)

	def test_reconciliation_delta_is_summed_per_item_and_warehouse(self):
		"""Several entries on one row (batch or serial) must add up, not overwrite each other."""
		entries = [
			_stub_sle("A", 3.0, 3.0),
			_stub_sle("A", 4.0, 7.0),  # same detail row, second entry
			_stub_sle("B", 0.0, 9.0, detail="d2"),  # counted quantity only in qty_after_transaction
		]
		doc = frappe._dict(doctype="Stock Reconciliation", name="SR-X")
		with (
			patch.object(stock_comptable, "_stock_ledger_entries_of", return_value=entries),
			patch.object(stock_comptable, "_quantity_before", return_value=5.0),
		):
			movements = {
				m["item_code"]: m["actual_qty"] for m in stock_comptable._reconciliation_movements(doc)
			}
		self.assertEqual(movements, {"A": 7.0, "B": 4.0})

	def test_reconciliation_of_a_row_with_inventory_dimensions_uses_counted_minus_current(self):
		"""ERPNext v16 writes the counted quantity as the entry quantity, with a balance of 0, for those rows."""
		entries = [_stub_sle("A", 12.0, 0.0)]
		row = frappe._dict(item_code="A", warehouse="W", qty=12.0, current_qty=9.0)
		doc = frappe._dict(doctype="Stock Reconciliation", name="SR-X", items=[row])
		with (
			patch.object(stock_comptable, "_stock_ledger_entries_of", return_value=entries),
			patch.object(stock_comptable, "_row_has_dimensions", return_value=True),
		):
			movements = stock_comptable._reconciliation_movements(doc)
		self.assertEqual([m["actual_qty"] for m in movements], [3.0])

	# --- rows without warehouse -------------------------------------------------------------

	def _row(self, **kwargs):
		base = {
			"idx": 1,
			"name": "row-1",
			"item_code": self.item_code,
			"warehouse": None,
			"dn_detail": None,
			"delivery_note": None,
			"stock_qty": 2.0,
			"qty": 2.0,
			"stock_uom": "Nos",
		}
		return frappe._dict({**base, **kwargs})

	def test_invoice_row_without_warehouse_is_refused(self):
		doc = frappe._dict(items=[self._row()], packed_items=[])
		with self.assertRaises(frappe.ValidationError) as caught:
			stock_comptable._sales_invoice_movements(doc)
		self.assertIn("entrepôt", str(caught.exception))

	def test_drop_shipped_row_needs_no_warehouse_and_moves_nothing(self):
		doc = frappe._dict(items=[self._row(delivered_by_supplier=1)], packed_items=[])
		self.assertEqual(stock_comptable._sales_invoice_movements(doc), [])

	def test_submit_refuses_a_row_without_warehouse_but_a_draft_can_be_saved(self):
		def invoice(action):
			return frappe._dict(
				items=[self._row()],
				packed_items=[],
				is_debit_note=0,
				is_return=0,
				update_stock=0,
				_action=action,
			)

		with self.change_settings(SETTINGS, ON):
			stock_comptable.validate_sales_invoice(invoice("save"))
			with self.assertRaises(frappe.ValidationError):
				stock_comptable.validate_sales_invoice(invoice("submit"))
		# Ledger off and no BL rule: nothing is checked, even on submit.
		with self.change_settings(SETTINGS, {**OFF, "require_delivery_note_before_invoice": 0}):
			stock_comptable.validate_sales_invoice(invoice("submit"))

	def test_packed_item_without_warehouse_is_refused(self):
		packed = frappe._dict(
			idx=1,
			item_code=self.item_code,
			qty=1.0,
			warehouse=None,
			uom="Nos",
			parent_detail_docname=None,
			name="p1",
		)
		with self.assertRaises(frappe.ValidationError):
			stock_comptable._sales_invoice_movements(frappe._dict(items=[], packed_items=[packed]))

	def test_row_with_warehouse_from_delivery_note_detail_is_accepted(self):
		row = self._row(dn_detail="dn-row-1", delivery_note="DN-1")
		real_get_value = frappe.db.get_value

		def get_value(doctype, *args, **kwargs):
			if doctype == "Delivery Note Item":
				return self.warehouse
			return real_get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			movements = stock_comptable._sales_invoice_movements(frappe._dict(items=[row], packed_items=[]))
		self.assertEqual(movements[0]["warehouse"], self.warehouse)
		self.assertEqual(movements[0]["actual_qty"], -2.0)

	# --- other vouchers that move stock -----------------------------------------------------

	def test_hooks_cover_every_voucher_that_moves_stock_outside_the_delivery_note(self):
		doc_events = frappe.get_hooks("doc_events")
		for doctype in (
			"Purchase Receipt",
			"Stock Entry",
			"Purchase Invoice",
			"Subcontracting Receipt",
			"Asset Capitalization",
		):
			for event, handler in (
				("on_submit", "trade_import.stock_comptable.on_submit_stock_voucher"),
				("on_cancel", "trade_import.stock_comptable.on_cancel_stock_voucher"),
			):
				handlers = doc_events[doctype][event]
				handlers = handlers if isinstance(handlers, list) else [handlers]
				self.assertIn(handler, handlers, f"{doctype} {event}")

	def test_purchase_invoice_with_update_stock_is_mirrored_and_reversed(self):
		supplier = "_Test SC Supplier"
		if not frappe.db.exists("Supplier", supplier):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": supplier,
					"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
				}
			).insert(ignore_permissions=True)
		expense = frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		with self.change_settings(SETTINGS, ON):
			pi = frappe.get_doc(
				{
					"doctype": "Purchase Invoice",
					"company": self.company,
					"supplier": supplier,
					"posting_date": nowdate(),
					"bill_no": "SC-PI-1",
					"bill_date": nowdate(),
					"update_stock": 1,
					# Set explicitly: a fresh site defaults the document currency to INR.
					"currency": self.currency,
					"conversion_rate": 1.0,
					"items": [
						{
							"item_code": self.item_code,
							"warehouse": self.warehouse,
							"qty": 5,
							"rate": 50,
							"expense_account": expense,
						}
					],
				}
			).insert()
			pi.submit()
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 5.0)
			self.assertEqual(self._comptable(), 5.0)
			pi.cancel()
			self.assertEqual(_get_real_stock_qty(self.item_code, self.warehouse), 0.0)
			self.assertEqual(self._comptable(), 0.0)

	# --- opening balance --------------------------------------------------------------------

	def test_initialise_seeds_missing_bins_from_real_stock_and_is_idempotent(self):
		with self.change_settings(SETTINGS, OFF):
			self._receive_stock(10)  # real stock moves, comptable does not
		with self.change_settings(SETTINGS, ON):
			self.assertEqual(self._comptable(), 0.0)
			first = stock_comptable.initialise_missing_bins(item_code=self.item_code)
			self.assertEqual(first["created"], 1)
			self.assertEqual(self._comptable(), 10.0)
			opening = frappe.get_all(
				"Stock Comptable Entry",
				filters={"item_code": self.item_code, "voucher_type": SETTINGS},
				fields=["actual_qty", "qty_after_transaction"],
			)
			self.assertEqual([(e.actual_qty, e.qty_after_transaction) for e in opening], [(10.0, 10.0)])
			second = stock_comptable.initialise_missing_bins(item_code=self.item_code)
			self.assertEqual(second["created"], 0)
			self.assertEqual(self._comptable(), 10.0)

	def test_initialise_never_overwrites_an_existing_bin(self):
		with self.change_settings(SETTINGS, ON):
			self._receive_stock(10)
			frappe.db.set_value("Stock Comptable Bin", {"item_code": self.item_code}, "actual_qty", 4.0)
			result = stock_comptable.initialise_missing_bins(item_code=self.item_code)
			self.assertEqual(result["created"], 0)
			self.assertEqual(self._comptable(), 4.0)

	def test_initialise_does_not_seed_negative_real_stock(self):
		with self.change_settings(SETTINGS, OFF):
			self._receive_stock(10)
		frappe.db.set_value(
			"Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty", -3
		)
		with self.change_settings(SETTINGS, ON):
			result = stock_comptable.initialise_missing_bins(item_code=self.item_code)
		self.assertEqual((result["created"], result["skipped_negative"]), (0, 1))
		self.assertFalse(
			frappe.db.exists(
				"Stock Comptable Bin", {"item_code": self.item_code, "warehouse": self.warehouse}
			)
		)

	def test_initialise_button_queues_the_seeding(self):
		with self.change_settings(SETTINGS, ON), patch.object(stock_comptable.frappe, "enqueue") as enqueue:
			result = stock_comptable.initialise_from_real_stock()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], "trade_import.stock_comptable.seed_in_background")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertEqual(result, {"queued": True})

	def test_initialise_button_needs_a_stock_or_system_manager(self):
		for user in ("Guest", self._user_with_role("Stock User")):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user):
				stock_comptable.initialise_from_real_stock()
		frappe.set_user(self._user_with_role("Stock Manager"))
		with (
			patch.object(stock_comptable.frappe, "enqueue"),
			patch.object(stock_comptable, "is_stock_comptable_enabled", return_value=True),
		):
			self.assertEqual(stock_comptable.initialise_from_real_stock(), {"queued": True})

	def test_initialise_button_refuses_when_the_ledger_is_disabled(self):
		with self.change_settings(SETTINGS, OFF):
			with self.assertRaises(frappe.ValidationError):
				stock_comptable.initialise_from_real_stock()

	def _settings_stub(self, was_on, is_on):
		settings = frappe._dict(enable_stock_comptable=is_on)
		settings.get_doc_before_save = lambda: frappe._dict(enable_stock_comptable=was_on)
		return settings

	def test_first_switch_on_queues_the_seeding(self):
		with (
			patch.object(stock_comptable, "ledger_has_entries", return_value=False),
			patch.object(stock_comptable, "enqueue_seeding") as enqueue,
			patch.object(frappe, "msgprint"),
		):
			REAL_TOGGLE_HANDLER(self._settings_stub(0, 1))
		enqueue.assert_called_once()

	def test_switch_on_with_existing_entries_only_warns(self):
		with (
			patch.object(stock_comptable, "ledger_has_entries", return_value=True),
			patch.object(stock_comptable, "enqueue_seeding") as enqueue,
			patch.object(frappe, "msgprint") as msgprint,
		):
			REAL_TOGGLE_HANDLER(self._settings_stub(0, 1))
		enqueue.assert_not_called()
		self.assertEqual(msgprint.call_args.kwargs["indicator"], "orange")

	def test_switch_off_warns_and_an_unchanged_flag_does_nothing(self):
		with (
			patch.object(stock_comptable, "enqueue_seeding") as enqueue,
			patch.object(frappe, "msgprint") as msgprint,
		):
			REAL_TOGGLE_HANDLER(self._settings_stub(1, 0))
			self.assertEqual(msgprint.call_count, 1)
			REAL_TOGGLE_HANDLER(self._settings_stub(1, 1))
			REAL_TOGGLE_HANDLER(self._settings_stub(0, 0))
			self.assertEqual(msgprint.call_count, 1)
		enqueue.assert_not_called()

	# --- one bin per pair -------------------------------------------------------------------

	def test_a_second_bin_for_the_same_pair_is_rejected_by_the_database(self):
		prev = frappe.flags.in_stock_comptable_posting
		frappe.flags.in_stock_comptable_posting = True
		try:
			stock_comptable._get_or_create_bin(self.item_code, self.warehouse, self.company, "Nos")
			duplicate = frappe.get_doc(
				{
					"doctype": "Stock Comptable Bin",
					"item_code": self.item_code,
					"warehouse": self.warehouse,
					"company": self.company,
					"stock_uom": "Nos",
					"actual_qty": 0,
				}
			)
			with self.assertRaises(frappe.exceptions.UniqueValidationError):
				duplicate.insert(ignore_permissions=True)
		finally:
			frappe.flags.in_stock_comptable_posting = prev

	def test_losing_a_concurrent_first_insert_reads_the_winners_bin(self):
		"""The first read misses (the winner commits just after it), the real unique key then rejects the insert."""
		prev = frappe.flags.in_stock_comptable_posting
		frappe.flags.in_stock_comptable_posting = True
		try:
			winner, _qty = stock_comptable._get_or_create_bin(
				self.item_code, self.warehouse, self.company, "Nos"
			)
			frappe.db.set_value("Stock Comptable Bin", winner, "actual_qty", 4.0)

			real_get_value = frappe.db.get_value
			missed = []

			def stale_first_read(doctype, *args, **kwargs):
				if doctype == "Stock Comptable Bin" and not missed:
					missed.append(1)
					return None
				return real_get_value(doctype, *args, **kwargs)

			with (
				patch.object(stock_comptable, "_qty_precision", return_value=3),
				patch.object(frappe.db, "get_value", side_effect=stale_first_read),
				patch.object(stock_comptable.frappe, "clear_messages") as clear_messages,
			):
				result = stock_comptable._get_or_create_bin(
					self.item_code, self.warehouse, self.company, "Nos"
				)
		finally:
			frappe.flags.in_stock_comptable_posting = prev
		self.assertEqual(result, (winner, 4.0))
		clear_messages.assert_called_once()

	def test_a_failed_insert_with_no_winner_to_read_is_raised(self):
		loser_doc = MagicMock()
		loser_doc.insert.side_effect = frappe.exceptions.UniqueValidationError(
			"Stock Comptable Bin", "x", Exception("dup")
		)
		with (
			patch.object(stock_comptable, "_qty_precision", return_value=3),
			patch.object(stock_comptable.frappe.db, "get_value", side_effect=[None, None]),
			patch.object(stock_comptable.frappe, "get_doc", return_value=loser_doc),
			patch.object(stock_comptable.frappe, "clear_messages"),
			self.assertRaises(frappe.exceptions.UniqueValidationError),
		):
			stock_comptable._get_or_create_bin("I", "W", "C", "Nos")

	def test_patch_is_idempotent_and_refuses_duplicates(self):
		unique_bin_patch.execute()  # the key already exists on this site: nothing to do
		chain = MagicMock()
		chain.select.return_value.groupby.return_value.having.return_value.run.return_value = [("I", "W")]
		with patch.object(unique_bin_patch.frappe.qb, "from_", return_value=chain):
			with self.assertRaises(frappe.ValidationError) as caught:
				unique_bin_patch.execute()
		self.assertIn("(I, W)", str(caught.exception))

	# --- lock order, net check, running balance ---------------------------------------------

	def test_bins_are_locked_in_sorted_order(self):
		calls = []

		def fake_bin(item_code, warehouse, company, stock_uom):
			calls.append((item_code, warehouse))
			return "BIN", 100.0

		voucher = frappe._dict(
			doctype="Stock Entry",
			name="SE-X",
			company=self.company,
			posting_date=nowdate(),
			posting_time="10:00:00",
		)
		moves = [
			{"item_code": "B", "warehouse": "W2", "stock_uom": "Nos", "actual_qty": -1},
			{"item_code": "A", "warehouse": "W1", "stock_uom": "Nos", "actual_qty": 2},
			{"item_code": "A", "warehouse": "W0", "stock_uom": "Nos", "actual_qty": 1},
		]
		with (
			patch.object(stock_comptable, "_get_or_create_bin", side_effect=fake_bin),
			patch.object(stock_comptable.frappe.db, "set_value"),
			patch.object(stock_comptable.frappe, "get_doc"),
		):
			stock_comptable._post_movements(moves, voucher)
		self.assertEqual(calls, [("A", "W0"), ("A", "W1"), ("B", "W2")])

	def test_negative_check_uses_the_net_movement_of_each_pair(self):
		with self.change_settings(SETTINGS, ON):
			se = self._receive_stock(2)
			voucher = frappe._dict(
				doctype="Stock Entry",
				name=se.name,
				company=self.company,
				posting_date=nowdate(),
				posting_time=nowtime(),
			)
			base = {"item_code": self.item_code, "warehouse": self.warehouse, "stock_uom": "Nos"}
			# Consume 3 and produce 3 of the same item in one warehouse: net 0, so the balance of 2
			# is never negative at the end, even though it dips to -1 between the two rows.
			stock_comptable._post_movements(
				[{**base, "actual_qty": -3.0}, {**base, "actual_qty": 3.0}], voucher
			)
			self.assertEqual(self._comptable(), 2.0)
			# A real net shortfall is still refused.
			with self.assertRaises(frappe.ValidationError):
				stock_comptable._post_movements([{**base, "actual_qty": -3.0}], voucher)
			self.assertEqual(self._comptable(), 2.0)

	def test_each_entry_records_the_running_balance_of_its_pair(self):
		with self.change_settings(SETTINGS, ON):
			se = self._receive_stock(2)
			voucher = frappe._dict(
				doctype="Stock Entry",
				name=se.name,
				company=self.company,
				posting_date=nowdate(),
				posting_time=nowtime(),
			)
			base = {"item_code": self.item_code, "warehouse": self.warehouse, "stock_uom": "Nos"}
			stock_comptable._post_movements(
				[{**base, "actual_qty": 3.0}, {**base, "actual_qty": -1.0}], voucher
			)
			entries = frappe.get_all(
				"Stock Comptable Entry",
				filters={"item_code": self.item_code},
				fields=["actual_qty", "qty_after_transaction"],
				order_by="name asc",
			)
		self.assertEqual(
			[(e.actual_qty, e.qty_after_transaction) for e in entries], [(2.0, 2.0), (3.0, 5.0), (-1.0, 4.0)]
		)
		self.assertEqual(self._comptable(), 4.0)

	def test_cancel_reverses_the_net_of_each_pair(self):
		with self.change_settings(SETTINGS, ON):
			se = self._receive_stock(5)
			self.assertEqual(self._comptable(), 5.0)
			se.cancel()
			self.assertEqual(self._comptable(), 0.0)
			entries = frappe.get_all(
				"Stock Comptable Entry",
				filters={"voucher_no": se.name},
				fields=["is_cancelled"],
			)
			self.assertTrue(entries and all(flt(e.is_cancelled) == 1 for e in entries))
