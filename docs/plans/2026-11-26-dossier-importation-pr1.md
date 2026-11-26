# Dossier Importation, PR 1: Dossier, item lines, links, statuses

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Dossier Importation" document per shipment that pulls goods from purchase orders, creates the purchase receipts, follows the shipment through its dates and receipts, and shows a computed status. Costs, the split engine, the landed cost voucher, pricing, reports and the role come in PRs 2 and 3, each with its own plan.

**Architecture:** Two new DocTypes in `trade_import` (the Dossier and its item lines). All behaviour lives in one module, `trade_import/dossier_flow.py`: pure functions (status, packing values) that are unit-tested without a database, and database functions (open order lines, adding lines, creating receipts, refreshing from receipts). Links to ERPNext documents are Custom Fields exported as fixtures; Purchase Receipt submit and cancel update the Dossier through `doc_events`. Spec: `docs/specs/2026-11-26-dossier-importation-design.md`.

**Tech Stack:** Frappe/ERPNext v16, Python 3.14, `frappe.qb`, `IntegrationTestCase` / `UnitTestCase`.

## Global Constraints

- `trade_import` is a **generic app**: it never imports, names or depends on `marone_custom`, `morocco_localization` or `hrms`; no client name or value anywhere.
- **No core edits.** Use Custom Fields (fixtures), `doc_events` and new DocTypes only.
- **Every UI customization is exported to this app's fixtures.**
- **User-facing text in French**, wrapped in `_()` in Python and `__()` in JavaScript.
- **Money:** `flt(value, precision)` with the field precision.
- **No dependency on the CGNC chart or the accountant** (parked): works on the Standard chart.
- **Python formatting:** tabs, line length as `ruff format` sets it (a hook runs it after each edit). Run `pre-commit run --all-files` in `apps/trade_import` before each commit.
- **Commits:** reference `CHW-106`; end the message with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`; never put the text of a forced-flag option in a message.
- **Test data is fictional.** `marone.localhost` holds real imported data: tests there must roll back (every test class here does), and nothing is created outside tests.

## Commands used in this plan

Run from `C:\Users\Chouchou\Desktop\Ahmed\Code Projects\MaroneERP` in Git Bash. `SITE` is `generic.localhost` or `marone.localhost`.

```bash
export MSYS_NO_PATHCONV=1
BENCH="docker compose -f fake-company-infra/dev/compose.yml exec -T frappe bash -lc"
$BENCH 'cd frappe-bench && bench --site SITE migrate'
$BENCH 'cd frappe-bench && bench --site SITE run-tests --module trade_import.tests.test_dossier_flow'
```

## File structure

| File | Responsibility |
| -- | -- |
| `trade_import/fixtures/custom_field.json` | Custom Fields: `dossier_importation` on Purchase Order, Purchase Order Item, Purchase Receipt, Purchase Invoice; `volume_par_unite` on Item |
| `trade_import/hooks.py` | `fixtures` hook; `doc_events` for Purchase Receipt submit and cancel |
| `trade_import/trade_import/doctype/dossier_importation/` | The Dossier: JSON, controller, form JS, list JS, connections |
| `trade_import/trade_import/doctype/dossier_importation_article/` | Child table: one row per purchase order line |
| `trade_import/dossier_flow.py` | All Dossier logic (pure functions plus database functions) |
| `trade_import/tests/test_dossier_flow.py` | Unit tests for the pure functions |
| `trade_import/tests/test_dossier_importation.py` | Integration tests for the flows |

## Interfaces (defined in Task 3 and later, used by later tasks)

```python
# trade_import/dossier_flow.py
STATUS_DRAFT = "Brouillon"; STATUS_ORDERED = "Commandé"; STATUS_IN_TRANSIT = "En transit"
STATUS_IN_CUSTOMS = "En dédouanement"; STATUS_RECEIVED = "Réceptionné"
STATUS_FINALISED = "Coûts finalisés"; STATUS_CLOSED = "Clôturé"
FROM_LIST = "Liste de colisage"; FROM_ITEM = "Fiche article"

def compute_status(has_lines: bool, departed: bool, arrived: bool, all_received: bool, current: str | None) -> str
def unit_weight_kg(weight_per_unit: float, weight_uom: str | None) -> float
def packing_values(source, weight, volume, stock_qty, unit_weight, unit_volume) -> tuple[float, float, str]
def available_qty(po_items: list[str], exclude_dossier: str | None = None) -> dict[str, float]
def get_open_order_lines(company: str, supplier: str | None = None) -> list[dict]      # whitelisted
def add_order_lines(dossier: str, po_items: list[str] | str) -> None                    # whitelisted
def create_receipts(dossier: str) -> list[str]                                          # whitelisted
def refresh_from_receipts(dossier: str, ignore_receipt: str | None = None) -> None
def receipt_on_submit(doc, method=None) -> None
def receipt_on_cancel(doc, method=None) -> None
```

---

### Task 1: Custom Fields, fixtures hook and the two DocTypes (the former Tasks 1 and 2, merged)

> The Custom Fields link to the DocType that Task 2 creates, so neither can migrate alone. Do Part 1, then Part 2, then run both sets of tests and commit once as Step 6 says.

#### Part 1: Custom Fields and the fixtures hook

**Files:**
- Create: `apps/trade_import/trade_import/fixtures/custom_field.json`
- Modify: `apps/trade_import/trade_import/hooks.py` (after `required_apps`, line 11)
- Test: `apps/trade_import/trade_import/tests/test_dossier_importation.py` (new, started here)

**Interfaces:**
- Produces: link field `dossier_importation` on `Purchase Order`, `Purchase Order Item`, `Purchase Receipt`, `Purchase Invoice`; Float field `volume_par_unite` on `Item`.

- [ ] **Step 1: Write the failing test**

Create `apps/trade_import/trade_import/tests/test_dossier_importation.py`:

```python
import frappe
from frappe.tests import IntegrationTestCase

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
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_importation'`
Expected: FAIL (`AssertionError: unexpectedly None`).

- [ ] **Step 3: Write the fixtures**

Create `apps/trade_import/trade_import/fixtures/custom_field.json`:

```json
[
 {"doctype": "Custom Field", "name": "Purchase Order-dossier_importation", "dt": "Purchase Order", "module": "Trade Import", "fieldname": "dossier_importation", "fieldtype": "Link", "label": "Dossier d'importation", "options": "Dossier Importation", "insert_after": "company", "read_only": 1, "allow_on_submit": 1, "in_standard_filter": 1, "no_copy": 1},
 {"doctype": "Custom Field", "name": "Purchase Order Item-dossier_importation", "dt": "Purchase Order Item", "module": "Trade Import", "fieldname": "dossier_importation", "fieldtype": "Link", "label": "Dossier d'importation", "options": "Dossier Importation", "insert_after": "item_name", "read_only": 1, "allow_on_submit": 1, "no_copy": 1},
 {"doctype": "Custom Field", "name": "Purchase Receipt-dossier_importation", "dt": "Purchase Receipt", "module": "Trade Import", "fieldname": "dossier_importation", "fieldtype": "Link", "label": "Dossier d'importation", "options": "Dossier Importation", "insert_after": "company", "read_only": 1, "allow_on_submit": 1, "in_standard_filter": 1, "no_copy": 1},
 {"doctype": "Custom Field", "name": "Purchase Invoice-dossier_importation", "dt": "Purchase Invoice", "module": "Trade Import", "fieldname": "dossier_importation", "fieldtype": "Link", "label": "Dossier d'importation", "options": "Dossier Importation", "insert_after": "company", "allow_on_submit": 1, "in_standard_filter": 1, "no_copy": 1},
 {"doctype": "Custom Field", "name": "Item-volume_par_unite", "dt": "Item", "module": "Trade Import", "fieldname": "volume_par_unite", "fieldtype": "Float", "label": "Volume par unité (m³)", "insert_after": "weight_uom", "precision": "6", "description": "Sert à répartir les frais d'importation au volume."}
]
```

Modify `apps/trade_import/trade_import/hooks.py`: after the line `required_apps = ["erpnext"]` add

```python
# Fixtures
# --------
# Every Desk customization of this app. Export with:
#   bench --site marone.localhost export-fixtures --app trade_import
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Purchase Order-dossier_importation",
					"Purchase Order Item-dossier_importation",
					"Purchase Receipt-dossier_importation",
					"Purchase Invoice-dossier_importation",
					"Item-volume_par_unite",
				],
			]
		],
	},
]
```

- [ ] **Step 4: Create the DocType stub the links point to, then migrate**

A Link to a missing DocType breaks the import, so Task 2 must exist first. **Do Task 2 before running this step**, then run:

`$BENCH 'cd frappe-bench && bench --site generic.localhost migrate'` and the test from Step 2.
Expected: PASS (2 tests).

- [ ] **Step 5: Commit** (after Task 2, together with it, to keep every commit migratable)

---

#### Part 2: The two DocTypes

**Files:**
- Create: `apps/trade_import/trade_import/trade_import/doctype/dossier_importation/__init__.py` (empty)
- Create: `.../dossier_importation/dossier_importation.json`
- Create: `.../dossier_importation/dossier_importation.py`
- Create: `apps/trade_import/trade_import/trade_import/doctype/dossier_importation_article/__init__.py` (empty)
- Create: `.../dossier_importation_article/dossier_importation_article.json`
- Create: `.../dossier_importation_article/dossier_importation_article.py`
- Test: `apps/trade_import/trade_import/tests/test_dossier_importation.py`

**Interfaces:**
- Produces: DocType `Dossier Importation` with fields `company`, `status`, `articles` (table), the date, transport and attachment fields, `total_valeur_mad`, `total_poids_kg`, `total_volume_m3`; child DocType `Dossier Importation Article` with the fields listed in the JSON below.

- [ ] **Step 1: Write the failing test** (append to `test_dossier_importation.py`)

```python
from trade_import.tests.test_stock_comptable import _ensure_test_master_data


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
```

- [ ] **Step 2: Run it to confirm it fails**

Run the module as in Task 1. Expected: FAIL (`DocType Dossier Importation not found`).

- [ ] **Step 3: Write the Dossier DocType**

`dossier_importation.json`:

```json
{
 "actions": [],
 "autoname": "format:DI-{YYYY}-{#####}",
 "creation": "2026-11-26 17:00:00",
 "doctype": "DocType",
 "engine": "InnoDB",
 "field_order": [
  "tab_expedition", "company", "status", "column_break_1", "transport_mode", "incoterm",
  "section_transport", "carrier", "forwarder", "column_break_2", "port_chargement", "port_arrivee",
  "section_references", "numero_bl_awb", "numero_dum", "column_break_3", "numeros_conteneurs",
  "section_dates", "date_depart_prevue", "date_arrivee_prevue", "date_dedouanement_prevue", "date_reception_prevue",
  "column_break_dates", "date_depart_reelle", "date_arrivee_reelle", "date_dedouanement_reelle", "date_reception_reelle",
  "section_pieces", "proforma", "facture_commerciale", "liste_colisage", "column_break_pieces", "bl_awb_fichier", "dum_fichier",
  "section_remarques", "remarques",
  "tab_articles", "articles", "section_totaux", "total_valeur_mad", "column_break_totaux", "total_poids_kg", "total_volume_m3"
 ],
 "fields": [
  {"fieldname": "tab_expedition", "fieldtype": "Tab Break", "label": "Expédition"},
  {"fieldname": "company", "fieldtype": "Link", "in_list_view": 1, "in_standard_filter": 1, "label": "Société", "options": "Company", "reqd": 1, "remember_last_selected_value": 1},
  {"default": "Brouillon", "fieldname": "status", "fieldtype": "Select", "in_list_view": 1, "in_standard_filter": 1, "label": "Statut", "no_copy": 1, "options": "Brouillon\nCommandé\nEn transit\nEn dédouanement\nRéceptionné\nCoûts finalisés\nClôturé", "read_only": 1},
  {"fieldname": "column_break_1", "fieldtype": "Column Break"},
  {"fieldname": "transport_mode", "fieldtype": "Select", "in_standard_filter": 1, "label": "Mode de transport", "options": "\nMaritime\nAérien\nRoutier\nMultimodal"},
  {"fieldname": "incoterm", "fieldtype": "Link", "label": "Incoterm", "options": "Incoterm"},
  {"fieldname": "section_transport", "fieldtype": "Section Break", "label": "Transport"},
  {"fieldname": "carrier", "fieldtype": "Link", "label": "Transporteur", "options": "Supplier"},
  {"fieldname": "forwarder", "fieldtype": "Link", "label": "Transitaire", "options": "Supplier"},
  {"fieldname": "column_break_2", "fieldtype": "Column Break"},
  {"fieldname": "port_chargement", "fieldtype": "Data", "label": "Port de chargement"},
  {"fieldname": "port_arrivee", "fieldtype": "Data", "in_standard_filter": 1, "label": "Port d'arrivée"},
  {"fieldname": "section_references", "fieldtype": "Section Break", "label": "Références"},
  {"fieldname": "numero_bl_awb", "fieldtype": "Data", "label": "N° BL / AWB"},
  {"fieldname": "numero_dum", "fieldtype": "Data", "label": "N° DUM"},
  {"fieldname": "column_break_3", "fieldtype": "Column Break"},
  {"fieldname": "numeros_conteneurs", "fieldtype": "Small Text", "label": "N° de conteneurs"},
  {"fieldname": "section_dates", "fieldtype": "Section Break", "label": "Dates"},
  {"fieldname": "date_depart_prevue", "fieldtype": "Date", "label": "Départ prévu"},
  {"fieldname": "date_arrivee_prevue", "fieldtype": "Date", "in_list_view": 1, "in_standard_filter": 1, "label": "Arrivée au port prévue"},
  {"fieldname": "date_dedouanement_prevue", "fieldtype": "Date", "label": "Dédouanement prévu"},
  {"fieldname": "date_reception_prevue", "fieldtype": "Date", "label": "Réception prévue"},
  {"fieldname": "column_break_dates", "fieldtype": "Column Break"},
  {"fieldname": "date_depart_reelle", "fieldtype": "Date", "label": "Départ réel"},
  {"fieldname": "date_arrivee_reelle", "fieldtype": "Date", "label": "Arrivée au port réelle"},
  {"fieldname": "date_dedouanement_reelle", "fieldtype": "Date", "label": "Dédouanement réel"},
  {"fieldname": "date_reception_reelle", "fieldtype": "Date", "label": "Réception réelle"},
  {"fieldname": "section_pieces", "fieldtype": "Section Break", "label": "Pièces du fournisseur"},
  {"fieldname": "proforma", "fieldtype": "Attach", "label": "Proforma"},
  {"fieldname": "facture_commerciale", "fieldtype": "Attach", "label": "Facture commerciale"},
  {"fieldname": "liste_colisage", "fieldtype": "Attach", "label": "Liste de colisage"},
  {"fieldname": "column_break_pieces", "fieldtype": "Column Break"},
  {"fieldname": "bl_awb_fichier", "fieldtype": "Attach", "label": "BL / AWB"},
  {"fieldname": "dum_fichier", "fieldtype": "Attach", "label": "DUM"},
  {"fieldname": "section_remarques", "fieldtype": "Section Break", "label": "Remarques"},
  {"fieldname": "remarques", "fieldtype": "Text Editor", "label": "Remarques"},
  {"fieldname": "tab_articles", "fieldtype": "Tab Break", "label": "Articles"},
  {"fieldname": "articles", "fieldtype": "Table", "label": "Articles", "options": "Dossier Importation Article"},
  {"fieldname": "section_totaux", "fieldtype": "Section Break", "label": "Totaux"},
  {"fieldname": "total_valeur_mad", "fieldtype": "Currency", "label": "Valeur des marchandises", "options": "Company:company:default_currency", "read_only": 1},
  {"fieldname": "column_break_totaux", "fieldtype": "Column Break"},
  {"fieldname": "total_poids_kg", "fieldtype": "Float", "label": "Poids total (kg)", "read_only": 1},
  {"fieldname": "total_volume_m3", "fieldtype": "Float", "label": "Volume total (m³)", "precision": "6", "read_only": 1}
 ],
 "index_web_pages_for_search": 1,
 "links": [],
 "modified": "2026-11-26 17:00:00",
 "modified_by": "Administrator",
 "module": "Trade Import",
 "name": "Dossier Importation",
 "owner": "Administrator",
 "permissions": [
  {"create": 1, "delete": 1, "email": 1, "print": 1, "read": 1, "report": 1, "role": "System Manager", "share": 1, "write": 1},
  {"create": 1, "email": 1, "print": 1, "read": 1, "report": 1, "role": "Purchase Manager", "share": 1, "write": 1},
  {"read": 1, "report": 1, "role": "Purchase User"}
 ],
 "sort_field": "modified",
 "sort_order": "DESC",
 "states": [],
 "title_field": "numero_bl_awb",
 "track_changes": 1
}
```

`dossier_importation.py` (a first version; Task 3 adds `validate`):

```python
from frappe.model.document import Document


class DossierImportation(Document):
	"""One shipment: its orders, receipts and costs. The logic is in trade_import.dossier_flow."""
```

`dossier_importation_dashboard.py` in the same folder (the "Documents" connections panel):

```python
from frappe import _


def get_data():
	return {
		"fieldname": "dossier_importation",
		"transactions": [
			{
				"label": _("Achats"),
				"items": ["Purchase Order", "Purchase Receipt", "Purchase Invoice"],
			},
		],
	}
```

- [ ] **Step 4: Write the item-line DocType**

`dossier_importation_article.json`:

```json
{
 "actions": [],
 "creation": "2026-11-26 17:00:00",
 "doctype": "DocType",
 "editable_grid": 1,
 "engine": "InnoDB",
 "field_order": [
  "purchase_order", "purchase_order_item", "supplier", "item_code", "item_name",
  "qty", "uom", "conversion_factor", "stock_qty",
  "currency", "rate", "exchange_rate", "amount", "valeur_mad",
  "poids_kg", "volume_m3", "source_colisage",
  "received_qty", "purchase_receipt"
 ],
 "fields": [
  {"fieldname": "purchase_order", "fieldtype": "Link", "in_list_view": 1, "label": "Commande", "options": "Purchase Order", "read_only": 1},
  {"fieldname": "purchase_order_item", "fieldtype": "Data", "hidden": 1, "label": "Ligne de commande", "read_only": 1, "search_index": 1},
  {"fieldname": "supplier", "fieldtype": "Link", "in_list_view": 1, "label": "Fournisseur", "options": "Supplier", "read_only": 1},
  {"fieldname": "item_code", "fieldtype": "Link", "in_list_view": 1, "label": "Article", "options": "Item", "read_only": 1},
  {"fieldname": "item_name", "fieldtype": "Data", "label": "Désignation", "read_only": 1},
  {"fieldname": "qty", "fieldtype": "Float", "in_list_view": 1, "label": "Quantité", "reqd": 1},
  {"fieldname": "uom", "fieldtype": "Link", "label": "UDM", "options": "UOM", "read_only": 1},
  {"default": "1", "fieldname": "conversion_factor", "fieldtype": "Float", "label": "Facteur de conversion", "read_only": 1},
  {"fieldname": "stock_qty", "fieldtype": "Float", "label": "Quantité en stock", "read_only": 1},
  {"fieldname": "currency", "fieldtype": "Link", "label": "Devise", "options": "Currency", "read_only": 1},
  {"fieldname": "rate", "fieldtype": "Float", "label": "Prix unitaire", "read_only": 1},
  {"fieldname": "exchange_rate", "fieldtype": "Float", "label": "Taux de change", "precision": "9", "read_only": 1},
  {"fieldname": "amount", "fieldtype": "Currency", "label": "Montant (devise de la commande)", "options": "currency", "read_only": 1},
  {"fieldname": "valeur_mad", "fieldtype": "Currency", "in_list_view": 1, "label": "Valeur (devise société)", "read_only": 1},
  {"fieldname": "poids_kg", "fieldtype": "Float", "in_list_view": 1, "label": "Poids brut (kg)"},
  {"fieldname": "volume_m3", "fieldtype": "Float", "in_list_view": 1, "label": "Volume (m³)", "precision": "6"},
  {"fieldname": "source_colisage", "fieldtype": "Select", "label": "Source poids / volume", "options": "\nListe de colisage\nFiche article"},
  {"default": "0", "fieldname": "received_qty", "fieldtype": "Float", "in_list_view": 1, "label": "Quantité reçue", "read_only": 1},
  {"fieldname": "purchase_receipt", "fieldtype": "Link", "label": "Dernière réception", "options": "Purchase Receipt", "read_only": 1}
 ],
 "index_web_pages_for_search": 1,
 "istable": 1,
 "links": [],
 "modified": "2026-11-26 17:00:00",
 "modified_by": "Administrator",
 "module": "Trade Import",
 "name": "Dossier Importation Article",
 "owner": "Administrator",
 "permissions": [],
 "sort_field": "modified",
 "sort_order": "DESC",
 "states": []
}
```

`dossier_importation_article.py`:

```python
from frappe.model.document import Document


class DossierImportationArticle(Document):
	pass
```

- [ ] **Step 5: Migrate both sites and run the tests**

```bash
$BENCH 'cd frappe-bench && bench --site generic.localhost migrate'
$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_importation'
```
Expected: migrate finishes without error; 4 tests pass (2 from Task 1, 2 from Task 2).
Then `bench --site marone.localhost migrate` and the same test module on `marone.localhost`. Expected: 4 tests pass.

- [ ] **Step 6: Commit** (Tasks 1 and 2 together)

```bash
cd apps/trade_import
git add trade_import/fixtures/custom_field.json trade_import/hooks.py trade_import/trade_import/doctype/dossier_importation trade_import/trade_import/doctype/dossier_importation_article trade_import/tests/test_dossier_importation.py
pre-commit run --all-files
git add -A && git commit -m "feat(CHW-106): Dossier Importation DocTypes and purchase links

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Pure functions: status and packing values

**Files:**
- Create: `apps/trade_import/trade_import/dossier_flow.py`
- Test: `apps/trade_import/trade_import/tests/test_dossier_flow.py`

**Interfaces:**
- Produces: the constants, `compute_status`, `unit_weight_kg`, `packing_values` exactly as in the Interfaces block above.

- [ ] **Step 1: Write the failing tests** — create `tests/test_dossier_flow.py`:

```python
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
```

- [ ] **Step 2: Run to confirm failure**

Run: `$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_flow'`
Expected: ERROR (`ModuleNotFoundError: trade_import.dossier_flow`).

- [ ] **Step 3: Write the minimal implementation** — create `trade_import/dossier_flow.py`:

```python
"""Dossier Importation: the logic behind one shipment (CHW-106).

Pure functions at the top (no database), database functions below. Spec:
docs/specs/2026-11-26-dossier-importation-design.md
"""

from frappe.utils import flt

STATUS_DRAFT = "Brouillon"
STATUS_ORDERED = "Commandé"
STATUS_IN_TRANSIT = "En transit"
STATUS_IN_CUSTOMS = "En dédouanement"
STATUS_RECEIVED = "Réceptionné"
STATUS_FINALISED = "Coûts finalisés"
STATUS_CLOSED = "Clôturé"

FROM_LIST = "Liste de colisage"
FROM_ITEM = "Fiche article"

# Set by the cost steps (later PRs), never taken away by the dates and receipts.
_LATER_STATUSES = (STATUS_FINALISED, STATUS_CLOSED)


def compute_status(
	has_lines: bool, departed: bool, arrived: bool, all_received: bool, current: str | None
) -> str:
	"""The status the dates and receipts give. A finalised or closed Dossier keeps its status."""
	if current in _LATER_STATUSES:
		return current
	if all_received and has_lines:
		return STATUS_RECEIVED
	if arrived:
		return STATUS_IN_CUSTOMS
	if departed:
		return STATUS_IN_TRANSIT
	return STATUS_ORDERED if has_lines else STATUS_DRAFT


def unit_weight_kg(weight_per_unit: float, weight_uom: str | None) -> float:
	"""The item card's unit weight, when it is in kilograms (or has no unit set)."""
	return flt(weight_per_unit) if weight_uom in (None, "", "Kg") else 0.0


def packing_values(
	source: str | None,
	weight: float,
	volume: float,
	stock_qty: float,
	unit_weight: float,
	unit_volume: float,
) -> tuple[float, float, str]:
	"""Weight (kg), volume (m³) and where they come from, for one item line.

	Values entered from the packing list are kept; an empty one is filled from the item card times
	the stock quantity. A line marked "Fiche article" is filled from the item card again.
	"""
	weight, volume = flt(weight), flt(volume)
	if source == FROM_ITEM:
		weight = volume = 0.0
	entered = bool(weight or volume)
	if not weight:
		weight = flt(unit_weight) * flt(stock_qty)
	if not volume:
		volume = flt(unit_volume) * flt(stock_qty)
	if entered:
		return weight, volume, FROM_LIST
	return weight, volume, FROM_ITEM if (weight or volume) else ""
```

- [ ] **Step 4: Run to confirm they pass**

Run the module as in Step 2. Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
cd apps/trade_import && pre-commit run --all-files
git add -A && git commit -m "feat(CHW-106): Dossier status and packing value rules

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Dossier validation: line values, totals, status

**Files:**
- Modify: `trade_import/dossier_flow.py` (append)
- Modify: `trade_import/trade_import/doctype/dossier_importation/dossier_importation.py`
- Test: `trade_import/tests/test_dossier_importation.py` (append)

**Interfaces:**
- Consumes: `compute_status`, `packing_values`, `unit_weight_kg`, constants from Task 3.
- Produces: `prepare_lines(doc)`, `set_totals(doc)`, `status_of(doc)`; `DossierImportation.validate` calls them.

- [ ] **Step 1: Write the failing tests** (append)

```python
from frappe.utils import flt, nowdate

from trade_import import dossier_flow as flow


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
```

Each test builds its lines by hand, so none needs a purchase order (those come in Task 5).

- [ ] **Step 2: Write the implementation** — append to `trade_import/dossier_flow.py`:

```python
# ---------------------------------------------------------------------------------------------
# Validation of a Dossier
# ---------------------------------------------------------------------------------------------

import frappe  # noqa: E402
from frappe import _  # noqa: E402


def prepare_lines(doc) -> None:
	"""Quantities, values and packing figures of every item line."""
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	codes = {row.item_code for row in doc.articles if row.item_code}
	items = {
		item.name: item
		for item in frappe.get_all(
			"Item",
			filters={"name": ("in", list(codes))},
			fields=["name", "weight_per_unit", "weight_uom", "volume_par_unite"],
		)
	}
	for row in doc.articles:
		row.stock_qty = flt(flt(row.qty) * flt(row.conversion_factor or 1), row.precision("stock_qty"))
		row.amount = flt(flt(row.qty) * flt(row.rate), row.precision("amount"))
		row.exchange_rate = 1.0 if row.currency == company_currency else flt(row.exchange_rate) or 1.0
		row.valeur_mad = flt(row.amount * row.exchange_rate, row.precision("valeur_mad"))
		item = items.get(row.item_code)
		row.poids_kg, row.volume_m3, row.source_colisage = packing_values(
			row.source_colisage,
			row.poids_kg,
			row.volume_m3,
			row.stock_qty,
			unit_weight_kg(item.weight_per_unit, item.weight_uom) if item else 0,
			flt(item.volume_par_unite) if item else 0,
		)
		row.poids_kg = flt(row.poids_kg, row.precision("poids_kg"))
		row.volume_m3 = flt(row.volume_m3, row.precision("volume_m3"))


def set_totals(doc) -> None:
	doc.total_valeur_mad = flt(
		sum(flt(row.valeur_mad) for row in doc.articles), doc.precision("total_valeur_mad")
	)
	doc.total_poids_kg = flt(sum(flt(row.poids_kg) for row in doc.articles), doc.precision("total_poids_kg"))
	doc.total_volume_m3 = flt(
		sum(flt(row.volume_m3) for row in doc.articles), doc.precision("total_volume_m3")
	)


def status_of(doc) -> str:
	rows = doc.articles
	all_received = bool(rows) and all(flt(row.received_qty, 6) >= flt(row.qty, 6) for row in rows)
	return compute_status(
		bool(rows),
		bool(doc.date_depart_reelle),
		bool(doc.date_arrivee_reelle),
		all_received,
		doc.status,
	)
```

Replace `dossier_importation.py` with:

```python
from frappe.model.document import Document

from trade_import import dossier_flow as flow


class DossierImportation(Document):
	"""One shipment: its orders, receipts and costs. The logic is in trade_import.dossier_flow."""

	def validate(self):
		flow.prepare_lines(self)
		flow.set_totals(self)
		self.status = flow.status_of(self)
```

- [ ] **Step 3: Move the imports to the top of the module**

The `import frappe` / `from frappe import _` lines appended above must sit with the other imports. Edit the top of `dossier_flow.py` so it reads:

```python
import frappe
from frappe import _
from frappe.utils import flt
```

and delete the two `# noqa: E402` lines and the section comment's import lines from the appended block. (`_` is used from Task 5 on; ruff will not flag it once Task 5 lands, so if `ruff` complains about the unused import now, add it in Task 5 instead.)

- [ ] **Step 4: Run the tests**

```bash
$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_flow'
$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_importation'
```
Expected: PASS (13 tests, then 8: the 4 from Tasks 1 and 2 plus these 4).

- [ ] **Step 5: Commit**

```bash
cd apps/trade_import && pre-commit run --all-files
git add -A && git commit -m "feat(CHW-106): Dossier line values, totals and status on save

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Open order lines and adding them to a Dossier

**Files:**
- Modify: `trade_import/dossier_flow.py` (append)
- Modify: `trade_import/trade_import/doctype/dossier_importation/dossier_importation.py` (add `check_lines` call)
- Test: `trade_import/tests/test_dossier_importation.py` (append)

**Interfaces:**
- Consumes: `prepare_lines`, `set_totals`, `status_of`.
- Produces: `available_qty`, `get_open_order_lines`, `add_order_lines`, `check_lines(doc)`.

- [ ] **Step 1: Write the failing tests** (append)

First the shared helpers (Task 5 is the first task that needs purchase orders):

```python
from unittest.mock import patch

from frappe.utils import add_days, flt, nowdate

from trade_import import dossier_flow as flow
from trade_import import foreign_payables

SAVEPOINT = "test_dossier_flow"


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
```

Then the tests:

```python
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
```

- [ ] **Step 2: Run to confirm failure**

Run: `$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_importation'`
Expected: ERROR/FAIL (`AttributeError: module 'trade_import.dossier_flow' has no attribute 'get_open_order_lines'`).

- [ ] **Step 3: Implement** — append to `trade_import/dossier_flow.py` (add `import json` at the top of the module):

```python
def available_qty(po_items: list[str], exclude_dossier: str | None = None) -> dict[str, float]:
	"""Quantity of each purchase order line that no Dossier has taken yet.

	Received outside any Dossier counts as taken; a Dossier line counts in full, received or not.
	`exclude_dossier` leaves one Dossier's own lines out, to check that Dossier on save.
	"""
	if not po_items:
		return {}
	order_rows = frappe.get_all(
		"Purchase Order Item",
		filters={"name": ("in", po_items)},
		fields=["name", "qty", "received_qty"],
	)
	dossier_rows = frappe.get_all(
		"Dossier Importation Article",
		filters={"purchase_order_item": ("in", po_items)},
		fields=["parent", "purchase_order_item", "qty", "received_qty"],
	)
	result = {}
	for order_row in order_rows:
		mine = [r for r in dossier_rows if r.purchase_order_item == order_row.name]
		received_in_dossiers = sum(flt(r.received_qty) for r in mine)
		taken = sum(flt(r.qty) for r in mine if r.parent != exclude_dossier)
		received_outside = flt(order_row.received_qty) - received_in_dossiers
		result[order_row.name] = flt(flt(order_row.qty) - received_outside - taken, 6)
	return result


@frappe.whitelist()
def get_open_order_lines(company: str, supplier: str | None = None) -> list[dict]:
	"""Purchase order lines still free to put in a Dossier, with the quantity left on each."""
	frappe.has_permission("Purchase Order", "read", throw=True)
	order = frappe.qb.DocType("Purchase Order")
	line = frappe.qb.DocType("Purchase Order Item")
	query = (
		frappe.qb.from_(line)
		.join(order)
		.on(line.parent == order.name)
		.select(
			line.name.as_("purchase_order_item"),
			line.parent.as_("purchase_order"),
			order.supplier,
			order.currency,
			order.conversion_rate,
			order.transaction_date,
			line.item_code,
			line.item_name,
			line.uom,
			line.conversion_factor,
			line.rate,
			line.qty,
			line.received_qty,
		)
		.where(
			(order.docstatus == 1)
			& (order.company == company)
			& (order.status.notin(["Closed", "On Hold", "Completed", "Cancelled"]))
			& (line.received_qty < line.qty)
		)
		.orderby(order.transaction_date)
	)
	if supplier:
		query = query.where(order.supplier == supplier)
	rows = query.run(as_dict=True)
	left = available_qty([row.purchase_order_item for row in rows])
	open_lines = []
	for row in rows:
		remaining = left.get(row.purchase_order_item, 0)
		if remaining > 0:
			row["qty"] = remaining
			open_lines.append(row)
	return open_lines


def check_lines(doc) -> None:
	"""Every line belongs to the company, and no purchase order line is over-allocated."""
	rows = [row for row in doc.articles if row.purchase_order_item]
	if not rows:
		return
	companies = dict(
		frappe.get_all(
			"Purchase Order",
			filters={"name": ("in", list({row.purchase_order for row in rows}))},
			fields=["name", "company"],
			as_list=True,
		)
	)
	wanted: dict[str, float] = {}
	for row in rows:
		if companies.get(row.purchase_order) != doc.company:
			frappe.throw(
				_("Ligne {0} : la commande {1} n'appartient pas à la société {2}.").format(
					row.idx, frappe.bold(row.purchase_order), frappe.bold(doc.company)
				)
			)
		wanted[row.purchase_order_item] = wanted.get(row.purchase_order_item, 0) + flt(row.qty)
	left = available_qty(list(wanted), exclude_dossier=None if doc.is_new() else doc.name)
	for po_item, qty in wanted.items():
		if flt(qty, 6) > flt(left.get(po_item, 0), 6):
			frappe.throw(
				_("La quantité de la ligne de commande {0} dépasse ce qui reste à expédier ({1}).").format(
					frappe.bold(po_item), left.get(po_item, 0)
				)
			)


@frappe.whitelist()
def add_order_lines(dossier: str, po_items: list[str] | str) -> None:
	"""Put the chosen purchase order lines in the Dossier, with the quantity left on each."""
	doc = frappe.get_doc("Dossier Importation", dossier)
	doc.check_permission("write")
	if isinstance(po_items, str):
		po_items = json.loads(po_items)
	open_lines = {line.purchase_order_item: line for line in get_open_order_lines(doc.company)}
	for name in po_items:
		line = open_lines.get(name)
		if not line:
			frappe.throw(_("La ligne {0} n'est plus disponible.").format(frappe.bold(name)))
		doc.append(
			"articles",
			{
				"purchase_order": line.purchase_order,
				"purchase_order_item": line.purchase_order_item,
				"supplier": line.supplier,
				"item_code": line.item_code,
				"item_name": line.item_name,
				"qty": line.qty,
				"uom": line.uom,
				"conversion_factor": line.conversion_factor,
				"currency": line.currency,
				"rate": line.rate,
				"exchange_rate": line.conversion_rate,
			},
		)
	doc.save()
	# The link on the order is a convenience for the connections panel; the item lines are the record.
	for name in po_items:
		frappe.db.set_value(
			"Purchase Order Item", name, "dossier_importation", doc.name, update_modified=False
		)
		frappe.db.set_value(
			"Purchase Order",
			open_lines[name].purchase_order,
			"dossier_importation",
			doc.name,
			update_modified=False,
		)
```

Add `flow.check_lines(self)` as the first line of `validate` in `dossier_importation.py`:

```python
	def validate(self):
		flow.check_lines(self)
		flow.prepare_lines(self)
		flow.set_totals(self)
		self.status = flow.status_of(self)
```

- [ ] **Step 4: Run to confirm they pass**

```bash
$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_importation'
```
Expected: PASS (all tests of Tasks 1, 2, 4 and 5: 4 + 4 + 7 = 15).

- [ ] **Step 5: Commit**

```bash
cd apps/trade_import && pre-commit run --all-files
git add -A && git commit -m "feat(CHW-106): pull open purchase order lines into a Dossier

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Creating the receipts and following them

**Files:**
- Modify: `trade_import/dossier_flow.py` (append)
- Modify: `trade_import/hooks.py` (`doc_events`)
- Test: `trade_import/tests/test_dossier_importation.py` (append)

**Interfaces:**
- Consumes: `add_order_lines`, `check_lines`, `status_of`.
- Produces: `create_receipts`, `refresh_from_receipts`, `receipt_on_submit`, `receipt_on_cancel`; Purchase Receipt documents carrying `dossier_importation`.

- [ ] **Step 1: Write the failing tests** (append)

```python
class TestReceipts(DossierTestCase):
	def loaded(self, *orders):
		dossier = self.dossier()
		flow.add_order_lines(dossier.name, [o.items[0].name for o in orders])
		dossier.reload()
		return dossier

	def test_one_draft_receipt_per_supplier_and_currency(self):
		a1, a2 = self.order(), self.order(qty=40)
		b = self.order("_Test DI Supplier B", "USD", 9.9)
		dossier = self.loaded(a1, a2, b)
		names = flow.create_receipts(dossier.name)
		self.assertEqual(len(names), 2)
		receipts = [frappe.get_doc("Purchase Receipt", n) for n in names]
		self.assertEqual({r.docstatus for r in receipts}, {0})
		self.assertEqual({r.dossier_importation for r in receipts}, {dossier.name})
		self.assertEqual(sorted(len(r.items) for r in receipts), [1, 2])

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
```

- [ ] **Step 2: Run to confirm failure**

Expected: FAIL (`AttributeError: ... 'create_receipts'`).

- [ ] **Step 3: Implement** — append to `trade_import/dossier_flow.py`:

```python
@frappe.whitelist()
def create_receipts(dossier: str) -> list[str]:
	"""Draft purchase receipts for what is not received yet: one per supplier and currency."""
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	doc = frappe.get_doc("Dossier Importation", dossier)
	doc.check_permission("write")
	drafts = frappe.get_all(
		"Purchase Receipt", filters={"dossier_importation": doc.name, "docstatus": 0}, pluck="name"
	)
	if drafts:
		frappe.throw(
			_("Un brouillon de réception existe déjà pour ce dossier : {0}.").format(", ".join(drafts))
		)
	# (supplier, currency) -> purchase order -> purchase order line -> quantity to receive
	pending: dict[tuple, dict[str, dict[str, float]]] = {}
	for row in doc.articles:
		to_receive = flt(row.qty) - flt(row.received_qty)
		if to_receive > 0 and row.purchase_order_item:
			orders = pending.setdefault((row.supplier, row.currency), {})
			orders.setdefault(row.purchase_order, {})[row.purchase_order_item] = to_receive
	if not pending:
		frappe.throw(_("Rien à réceptionner : toutes les lignes sont reçues."))
	created = []
	for orders in pending.values():
		receipt = None
		for order, lines in orders.items():
			receipt = make_purchase_receipt(
				order, target_doc=receipt, args={"filtered_children": list(lines)}
			)
		wanted = {name: qty for lines in orders.values() for name, qty in lines.items()}
		receipt.dossier_importation = doc.name
		for item in receipt.items:
			item.qty = wanted[item.purchase_order_item]
		receipt.insert()
		created.append(receipt.name)
	return created


def refresh_from_receipts(dossier: str, ignore_receipt: str | None = None) -> None:
	"""Received quantities, receipt references and rates of the Dossier lines, from the receipts.

	Only submitted receipts linked to the Dossier count. `ignore_receipt` leaves one out, for the
	receipt being cancelled.
	"""
	doc = frappe.get_doc("Dossier Importation", dossier)
	po_items = [row.purchase_order_item for row in doc.articles if row.purchase_order_item]
	if not po_items:
		return
	company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")
	receipt = frappe.qb.DocType("Purchase Receipt")
	item = frappe.qb.DocType("Purchase Receipt Item")
	query = (
		frappe.qb.from_(item)
		.join(receipt)
		.on(item.parent == receipt.name)
		.select(
			item.purchase_order_item,
			item.qty,
			receipt.name.as_("receipt"),
			receipt.conversion_rate,
		)
		.where(
			(receipt.docstatus == 1)
			& (receipt.dossier_importation == dossier)
			& (item.purchase_order_item.isin(po_items))
		)
		.orderby(receipt.posting_date)
		.orderby(receipt.creation)
	)
	if ignore_receipt:
		query = query.where(receipt.name != ignore_receipt)
	received: dict[str, list] = {}
	for entry in query.run(as_dict=True):
		received.setdefault(entry.purchase_order_item, []).append(entry)
	order_rates = dict(
		frappe.get_all(
			"Purchase Order",
			filters={"name": ("in", list({row.purchase_order for row in doc.articles}))},
			fields=["name", "conversion_rate"],
			as_list=True,
		)
	)
	for row in doc.articles:
		entries = received.get(row.purchase_order_item, [])
		row.received_qty = sum(flt(entry.qty) for entry in entries)
		row.purchase_receipt = entries[-1].receipt if entries else None
		if row.currency != company_currency:
			row.exchange_rate = (
				flt(entries[-1].conversion_rate) if entries else flt(order_rates.get(row.purchase_order))
			)
	doc.save(ignore_permissions=True)


def receipt_on_submit(doc, method=None) -> None:
	if doc.get("dossier_importation"):
		refresh_from_receipts(doc.dossier_importation)


def receipt_on_cancel(doc, method=None) -> None:
	if doc.get("dossier_importation"):
		refresh_from_receipts(doc.dossier_importation, ignore_receipt=doc.name)
```

Modify `hooks.py` `doc_events`: add this entry next to the others

```python
	"Purchase Receipt": {
		"on_submit": "trade_import.dossier_flow.receipt_on_submit",
		"on_cancel": "trade_import.dossier_flow.receipt_on_cancel",
	},
```

- [ ] **Step 4: Run to confirm they pass**

```bash
$BENCH 'cd frappe-bench && bench --site generic.localhost run-tests --module trade_import.tests.test_dossier_importation'
```
Expected: PASS (all 22 tests of this module). If `test_a_partial_receipt_keeps_the_dossier_open` fails on the second receipt's quantity, print `receipt.items[0].qty` right after `create_receipts`: ERPNext's mapper may fill `qty` from the order's pending quantity, which is why `create_receipts` overwrites it from the Dossier line; the fix belongs in `create_receipts`, not in the test.

- [ ] **Step 5: Commit**

```bash
cd apps/trade_import && pre-commit run --all-files
git add -A && git commit -m "feat(CHW-106): create receipts from a Dossier and follow them

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: The form, list and connections

**Files:**
- Create: `trade_import/trade_import/doctype/dossier_importation/dossier_importation.js`
- Create: `trade_import/trade_import/doctype/dossier_importation/dossier_importation_list.js`

**Interfaces:**
- Consumes: whitelisted `get_open_order_lines`, `add_order_lines`, `create_receipts` (by dotted path `trade_import.dossier_flow.<name>`).

- [ ] **Step 1: Write the form script** — `dossier_importation.js`:

```javascript
frappe.ui.form.on("Dossier Importation", {
	refresh(frm) {
		if (frm.is_new()) return;
		const group = __("Actions");
		frm.add_custom_button(__("Récupérer les commandes"), () => show_order_dialog(frm), group);
		const to_receive = (frm.doc.articles || []).some((row) => row.qty > row.received_qty);
		if (to_receive) {
			frm.add_custom_button(__("Créer la réception"), () => create_receipts(frm), group);
		}
	},
});

frappe.ui.form.on("Dossier Importation Article", {
	// A figure typed by hand comes from the packing list, not from the item card any more.
	poids_kg(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "source_colisage", "Liste de colisage");
	},
	volume_m3(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "source_colisage", "Liste de colisage");
	},
});

function show_order_dialog(frm) {
	frappe.call({
		method: "trade_import.dossier_flow.get_open_order_lines",
		args: { company: frm.doc.company },
		callback: ({ message }) => {
			const lines = message || [];
			if (!lines.length) {
				frappe.msgprint(__("Aucune ligne de commande disponible pour cette société."));
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Récupérer les commandes"),
				size: "extra-large",
				fields: [
					{
						fieldname: "lines",
						fieldtype: "Table",
						cannot_add_rows: true,
						cannot_delete_rows: true,
						in_place_edit: false,
						data: lines,
						fields: [
							{ fieldname: "purchase_order", fieldtype: "Link", options: "Purchase Order", label: __("Commande"), in_list_view: 1, read_only: 1 },
							{ fieldname: "supplier", fieldtype: "Link", options: "Supplier", label: __("Fournisseur"), in_list_view: 1, read_only: 1 },
							{ fieldname: "item_code", fieldtype: "Link", options: "Item", label: __("Article"), in_list_view: 1, read_only: 1 },
							{ fieldname: "qty", fieldtype: "Float", label: __("Reste"), in_list_view: 1, read_only: 1 },
							{ fieldname: "currency", fieldtype: "Link", options: "Currency", label: __("Devise"), in_list_view: 1, read_only: 1 },
						],
					},
				],
				primary_action_label: __("Ajouter"),
				primary_action() {
					const chosen = dialog.fields_dict.lines.grid.get_selected_children();
					if (!chosen.length) {
						frappe.msgprint(__("Cochez au moins une ligne."));
						return;
					}
					frappe.call({
						method: "trade_import.dossier_flow.add_order_lines",
						args: { dossier: frm.doc.name, po_items: chosen.map((row) => row.purchase_order_item) },
						freeze: true,
						callback: () => {
							dialog.hide();
							frm.reload_doc();
						},
					});
				},
			});
			dialog.show();
		},
	});
}

function create_receipts(frm) {
	frappe.call({
		method: "trade_import.dossier_flow.create_receipts",
		args: { dossier: frm.doc.name },
		freeze: true,
		callback: ({ message }) => {
			frappe.msgprint(__("Réceptions créées en brouillon : {0}", [(message || []).join(", ")]));
			frm.reload_doc();
		},
	});
}
```

- [ ] **Step 2: Write the list script** — `dossier_importation_list.js`:

```javascript
frappe.listview_settings["Dossier Importation"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			"Brouillon": "gray",
			"Commandé": "blue",
			"En transit": "orange",
			"En dédouanement": "yellow",
			"Réceptionné": "green",
			"Coûts finalisés": "purple",
			"Clôturé": "darkgrey",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};
```

- [ ] **Step 3: Build and check by hand in the desk**

```bash
$BENCH 'cd frappe-bench && bench build --app trade_import && bench --site generic.localhost clear-cache'
```
Then open `http://generic.localhost:8000/app/dossier-importation` (Administrator / admin), create a Dossier, and check:
1. The form shows the tabs Expédition and Articles, and the Totaux section.
2. "Actions → Récupérer les commandes" lists open lines (create a purchase order first if the site has none), and "Ajouter" fills the Articles table and the totals.
3. "Créer la réception" appears while quantities are open and creates a draft receipt; submitting it flips the status to "Réceptionné".
4. The list shows a coloured status; the Dossier's connections panel counts the order and receipt.
Expected: all four hold. Delete the test records afterwards (generic.localhost holds no real data).

- [ ] **Step 4: Commit**

```bash
cd apps/trade_import && pre-commit run --all-files
git add -A && git commit -m "feat(CHW-106): Dossier form, list and order dialog

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Verify, amend the spec, review and open the PR

**Files:**
- Modify: `apps/trade_import/docs/specs/2026-11-26-dossier-importation-design.md`

- [ ] **Step 1: Full test runs**

```bash
$BENCH 'cd frappe-bench && for s in generic.localhost marone.localhost; do bench --site $s migrate && bench --site $s run-tests --app trade_import 2>&1 | grep -E "^(OK|Ran|FAILED)"; done'
```
Expected: `OK` on both sites; the whole `trade_import` suite (54 earlier tests plus 35 new ones). Then check that no test records remain on `marone.localhost`:

```bash
$BENCH 'cd frappe-bench && bench --site marone.localhost mariadb -e "select count(*) from \`tabDossier Importation\`; select count(*) from tabSupplier where name like \"_Test DI%%\""'
```
Expected: `0` and `0`.

- [ ] **Step 2: Fixtures round trip**

```bash
$BENCH 'cd frappe-bench && bench --site marone.localhost export-fixtures --app trade_import'
cd apps/trade_import && git status --short
```
Expected: `trade_import/fixtures/custom_field.json` may be reformatted by Frappe; if so, read the diff (it must hold the same five fields) and keep the exported version. Nothing else changes.

- [ ] **Step 3: Amend the spec for what PR 1 settled**

In `docs/specs/2026-11-26-dossier-importation-design.md`, replace the line

`- A user can move it back by hand, except to "Clôturé", whose rule above always applies.`

with

`- The status is always computed, so nothing is moved by hand. A finalised or closed Dossier keeps its status (set by the cost step and by closing, in later PRs).`

and add under "Data model", after the Article list:

`- A line's packing figures carry a source: "Liste de colisage" when typed, "Fiche article" when taken from the item card. A line on "Fiche article" follows the item card again at every save. The item card's unit weight is used only when it is in kilograms (or has no unit).`
`- The Dossier link on a purchase order and its lines names the latest Dossier that took the line; the Article rows are the record.`
`- A line received in several receipts shows the last receipt and the total received quantity; the receipts themselves are found from the Purchase Receipt Item rows.`

- [ ] **Step 4: Reviews**

Run the `frappe-reviewer` subagent on `git -C apps/trade_import diff main...HEAD`, and the `accounting-checker` on the receipt and value handling (valuation is untouched in this PR, but the MAD value and receipt-rate logic feed PR 2). Fix every finding, rerun Step 1, and commit.

- [ ] **Step 5: Push and open the PR**

```bash
cd apps/trade_import
git add -A && git commit -m "docs(CHW-106): settle PR 1 details in the Dossier spec

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
git push -u origin chw-106-dossier-importation
gh pr create --repo r0b0t007/trade_import --base main --title "CHW-106: Dossier Importation, item lines and receipts" --body-file <description file>
```

The description lists: what changed (the two DocTypes, the Custom Fields, the flows, the statuses), what was verified (test counts on both sites, the manual desk check of Task 7, no leftovers on `marone.localhost`), what is not verified (CI on this branch, a fresh-site install), and what comes next (PR 2: costs and the landed cost voucher; PR 3: pricing, reports, role). End it with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Wait for green CI before merging.

---

## Self-review

**Spec coverage (PR 1 scope).**

| Spec item | Task |
| -- | -- |
| Dossier header, dates, attachments, naming | 2 |
| Article lines with PO links, currency, rate, value in company currency | 2, 4, 5 |
| Weight and volume: packing list, item card fallback, item volume field | 1, 3, 4 |
| Custom Fields on PO, PO line, receipt, invoice, Item; fixtures | 1 |
| Récupérer les commandes | 5, 7 |
| Créer la réception, one per supplier and currency, draft | 6, 7 |
| Receipt submit and cancel update the Dossier; value at the receipt's rate | 6 |
| Statuses computed from dates and receipts | 3, 4 |
| Over-allocation rule, company rule, short shipments | 5 |
| Form, list colours, connections panel | 2 (dashboard file), 7 |

Not in this plan on purpose (PR 2 and 3): cost types and lines, cost invoice tracking and `type_frais_importation`, the split engine, Finaliser les coûts, the landed cost voucher link, pricing, reports, the role. The `Landed Cost Voucher` Custom Field belongs with PR 2.

**Placeholder scan.** No TBD or "similar to" steps; every code step has the code. The one hand-run check is Task 7 Step 3 (the desk), because the form script cannot be unit-tested here.

**Type consistency.** `flow.add_order_lines(dossier, po_items)`, `flow.get_open_order_lines(company, supplier=None)`, `flow.available_qty(po_items, exclude_dossier=None)`, `flow.create_receipts(dossier)`, `flow.refresh_from_receipts(dossier, ignore_receipt=None)` and `compute_status(has_lines, departed, arrived, all_received, current)` are used with the same names and argument order in the tests, hooks and the form script.

**Known risks to watch while executing.**
- Task 4 appends `import frappe` mid-module; Step 3 moves it to the top. If `ruff` flags `_` as unused before Task 5, add it in Task 5.
- `test_every_line_must_belong_to_the_dossier_company` creates a second company, which makes ERPNext create accounts and warehouses; the savepoint rollback removes them, but the Warehouse Type "Transit" must exist (it does after `_ensure_test_master_data`).
- ERPNext's `make_purchase_receipt` may fill `received_qty` or `qty` differently on a second call with `target_doc`; `create_receipts` overwrites `qty` from the Dossier line, and Task 6 Step 4 says where to look if a test disagrees.
