from frappe.tests import UnitTestCase

from trade_import import dossier_flow as flow


class TestComputeStatus(UnitTestCase):
	def status(self, has_lines=True, departed=False, arrived=False, all_received=False, current=None):
		return flow.compute_status(has_lines, departed, arrived, all_received, current)

	def test_no_lines_is_a_draft(self):
		self.assertEqual(self.status(has_lines=False), flow.STATUS_DRAFT)

	def test_lines_make_it_ordered(self):
		self.assertEqual(self.status(), flow.STATUS_ORDERED)

	def test_departure_makes_it_in_transit(self):
		self.assertEqual(self.status(departed=True), flow.STATUS_IN_TRANSIT)

	def test_arrival_makes_it_in_customs(self):
		self.assertEqual(self.status(departed=True, arrived=True), flow.STATUS_IN_CUSTOMS)

	def test_full_reception_wins_over_the_dates(self):
		self.assertEqual(self.status(all_received=True), flow.STATUS_RECEIVED)

	def test_later_statuses_are_kept(self):
		for current in (flow.STATUS_FINALISED, flow.STATUS_CLOSED):
			self.assertEqual(self.status(all_received=True, current=current), current)
			self.assertEqual(self.status(has_lines=False, current=current), current)


class TestUnitWeight(UnitTestCase):
	def test_kilograms_or_unset_unit_are_used(self):
		self.assertEqual(flow.unit_weight_kg(2.5, "Kg"), 2.5)
		self.assertEqual(flow.unit_weight_kg(2.5, None), 2.5)
		self.assertEqual(flow.unit_weight_kg(2.5, ""), 2.5)

	def test_other_units_are_ignored(self):
		self.assertEqual(flow.unit_weight_kg(2.5, "Gram"), 0.0)


class TestPackingValues(UnitTestCase):
	def test_entered_values_are_kept_and_marked_as_packing_list(self):
		self.assertEqual(flow.packing_values("", 120.0, 0.8, 100, 1.0, 0.01), (120.0, 0.8, flow.FROM_LIST))

	def test_empty_values_come_from_the_item_card(self):
		self.assertEqual(flow.packing_values("", 0, 0, 100, 1.5, 0.002), (150.0, 0.2, flow.FROM_ITEM))

	def test_a_missing_dimension_is_filled_from_the_item_card(self):
		weight, volume, source = flow.packing_values("", 120.0, 0, 100, 1.0, 0.002)
		self.assertEqual((weight, volume, source), (120.0, 0.2, flow.FROM_LIST))

	def test_rows_marked_item_card_follow_the_item_card_again(self):
		self.assertEqual(
			flow.packing_values(flow.FROM_ITEM, 150.0, 0.2, 200, 1.5, 0.002), (300.0, 0.4, flow.FROM_ITEM)
		)

	def test_nothing_known_gives_zero_and_no_source(self):
		self.assertEqual(flow.packing_values("", 0, 0, 100, 0, 0), (0.0, 0.0, ""))
