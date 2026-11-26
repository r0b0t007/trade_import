# Dossier Importation: design

Linear: CHW-106. App: `trade_import` (generic, ADR 0001). Approved with the owner on 2026-11-26.

## Goal

One document per shipment (one container or air consignment) that the import team works from, start to finish:

1. Pull in the goods from one or more purchase orders.
2. Track the shipment through transport and customs.
3. Collect the import costs, estimated and then actual.
4. Spread the costs over the items, each cost with its own method.
5. Post the real landed cost into stock valuation through ERPNext's Landed Cost Voucher.
6. Show the landed cost per item, and the selling price at a target margin.

## Decisions

| Topic | Decision |
| -- | -- |
| Cost entry | Estimated cost lines on the Dossier when ordering, replaced by the actual costs from the purchase invoices linked to the Dossier. |
| Cost split | One method per cost line: value, quantity, weight, volume, percentage, or a manual amount per item. |
| Weight and volume | Entered per line from the packing list. When empty, taken from the item card × quantity: weight from ERPNext's `weight_per_unit`, volume from a new item field. |
| Shipment content | Groupage: several suppliers and currencies in one Dossier. Each line keeps its order's currency and exchange rate. |
| Selling prices | The Dossier suggests a price at a target margin %. A button updates a chosen price list after review. Nothing changes on its own. |
| Valuation | ERPNext's Landed Cost Voucher with "Distribute Manually", filled with the Dossier's computed amounts. ERPNext posts the GL and stock entries and reposts. |
| Accounting choices | Standard ERPNext accounts ("Expenses Included In Valuation"). No dependency on the CGNC chart or the accountant (parked, CHW-101). |

## Data model

### Dossier Importation (DocType, not submittable)

- **Naming:** `DI-.YYYY.-.#####`.
- **Header:**
  - company, status (see below);
  - transport mode (Maritime, Aérien, Routier, Multimodal), Incoterm (ERPNext's Incoterm DocType);
  - carrier, forwarder (both Supplier), ports of loading and arrival (Data);
  - BL/AWB number, DUM number, container numbers (Small Text);
  - target margin % (default from Trade Import Settings);
  - remarks.
- **Dates** (expected and actual): departure, arrival at port, customs clearance, receipt.
- **Attachments:** proforma, commercial invoice, packing list, BL/AWB, DUM (Attach fields), plus the standard attachments.

### Dossier Importation Article (child table)

- **Purchase links:** purchase order, purchase order item (Data, the PO Item row), supplier, item, description.
- **Quantity:** qty, UOM, stock qty, conversion factor.
- **Value:**
  - currency, unit price, exchange rate (from the order);
  - amount in the order currency;
  - **value in MAD**: the company currency, at the order's rate until the receipt, then at the receipt's rate.
- **Packing list:** gross weight (kg) and volume (m³). When empty, filled from the item card × stock qty, with a "source" flag (Liste de colisage / Fiche article).
- **Receipt:** received qty, purchase receipt, purchase receipt item.
- **Cost results** (read-only):
  - estimated and actual cost share;
  - landed cost per stock unit, estimated and actual.
- **Pricing** (read-only): current selling price (from the selected price list), suggested price at the target margin.

### Dossier Importation Frais (child table)

- **Cost:** cost type (Link to "Type de frais d'importation"), description, split method.
- **Amounts:**
  - estimated amount in MAD;
  - actual amount in MAD (read-only, the sum of the linked purchase invoice lines);
  - difference;
  - "keep the estimate" checkbox, for a cost that will never be invoiced.
- **Manual and percentage splits:** a JSON field of item row → percentage or amount, edited in a small dialog.

### Type de frais d'importation (DocType)

- **Fields:** name, default split method, and the default expense item used on cost invoices.
- **Fixtures:** Fret international (poids), Assurance (valeur), Droits de douane (valeur), Transit / dédouanement (valeur), Magasinage / frais de port (volume), Transport local (poids), Frais bancaires (valeur), Commission (valeur), Autre (valeur).

### Custom Fields (exported to fixtures)

- `dossier_importation` (Link) on:
  - Purchase Order, and Purchase Order Item (one order can feed two Dossiers);
  - Purchase Receipt, Purchase Invoice, Landed Cost Voucher.
- `type_frais_importation` (Link) on Purchase Invoice Item, so each cost invoice line names its cost type.
- `volume_par_unite` (Float, m³) on Item.

### Trade Import Settings (existing, new fields)

- Default target margin %.
- Default selling price list for the suggestions.

## Status

The steps are: Brouillon → Commandé → En transit → En dédouanement → Réceptionné → Coûts finalisés → Clôturé.

| Status | When |
| -- | -- |
| Brouillon | The Dossier has no lines yet. |
| Commandé | It has lines from submitted orders. |
| En transit | The actual departure date is set. |
| En dédouanement | The actual arrival date is set. |
| Réceptionné | Every line is fully received. |
| Coûts finalisés | A submitted Landed Cost Voucher covers every cost line. |
| Clôturé | Set by hand. Allowed only from "Coûts finalisés", with no linked document left in draft. |

- The status is computed on save from the dates and the linked documents.
- A user can move it back by hand, except to "Clôturé", whose rule above always applies.
- Cancelling a linked Landed Cost Voucher sends the status back to "Réceptionné".

## Flows

### 1. Récupérer les commandes

- A dialog lists the open purchase order lines: submitted orders, not fully received, not already in another Dossier for the same quantity.
- It filters by supplier and company.
- The chosen lines become Article rows, and the order and order lines get the Dossier link.

### 2. Créer la réception

- One Purchase Receipt per supplier, from the Article rows not yet received, using ERPNext's mapper from the order.
- Each receipt carries the Dossier link, and saves as a draft for the user to check and submit.
- On submit of a receipt linked to a Dossier, a doc_event updates the received qty, the receipt references, the value in MAD at the receipt rate, and the status.

### 3. Cost invoices

- Each cost (forwarder, customs, transport, bank) is a normal Purchase Invoice with the Dossier set.
- Each invoice line names its cost type, with ERPNext's "Expenses Included In Valuation" account by default.
- On submit or cancel of such an invoice, a doc_event recomputes each cost line's actual amount: the sum, in MAD, of the submitted invoice lines with that cost type. A cost type with no cost line on the Dossier is added as a new line, using the type's default method.
- The supplier's goods invoice can carry the Dossier link for reference. Its lines have no cost type, so it does not count as a cost.

### 4. Finaliser les coûts

**Allowed when:**
- every line is received. A line may be received in several receipts. When less was shipped than ordered, the user lowers the line quantity to what was shipped first; the rest of the order line stays open for another Dossier;
- every cost line has an actual amount, or is marked "keep the estimate".

**Steps:**
1. Compute each cost line's share per Article row (see Split engine). An actual amount is used when present, otherwise the kept estimate.
2. Create the Landed Cost Voucher:
   - its purchase receipts table lists the Dossier's receipts;
   - one item row per received receipt item. When a line was received in several receipts, its share is split between them by received quantity;
   - the charges are grouped by expense account;
   - the vendor invoices table lists the linked cost invoices;
   - "Distribute Manually", with `applicable_charges` per item row set from step 1.
3. Save it as a draft linked to the Dossier and open it for review. The user submits it.

**Late costs:** a later run finds the cost already covered by submitted vouchers linked to the Dossier, and creates a voucher for the difference only.

### 5. Prix

- **Read-only per row:**
  - landed cost per stock unit;
  - the current price on the chosen price list;
  - the suggested price = landed cost per unit × (1 + target margin %), rounded to the currency precision.
- **"Mettre à jour la liste de prix":**
  - a dialog lists the suggested prices, with checkboxes and editable values;
  - it writes Item Price records only for the rows ticked;
  - it never runs on its own.

## Split engine

A pure Python module, `trade_import/landed_cost_split.py`, with no database access:

- **Input:** the Article rows (key, value in MAD, stock qty, weight, volume), the cost amount, the method, and the manual or percentage values.
- **Output:** the amount per row key.
- **Methods:**
  - value, quantity, weight and volume are proportional to that field;
  - percentage uses the given percentages, which must add up to 100 (tolerance 0.01);
  - manual uses the given amounts, which must add up to the cost.
- **Rounding:** each share is rounded to the currency precision, and the remainder goes to the row with the largest basis. The shares then add up exactly to the cost.
- **Errors (raised, with a French message for the form):**
  - the basis total is zero (for example a weight split with every weight empty);
  - percentages or manual amounts don't add up;
  - a negative basis.

## Screens

**Dossier form tabs:**
- **Expédition:** header, transport, dates and attachments.
- **Articles:** the lines and the totals (value in MAD, weight, volume).
- **Coûts:**
  - the cost lines;
  - the summary: goods value, estimated and actual costs, cost % of goods value.
- **Coût de revient:** landed cost and pricing per item.
- **Documents:** ERPNext's connections panel.

**Buttons** appear only when the step applies:
- Récupérer les commandes;
- Créer la réception;
- Finaliser les coûts;
- Mettre à jour la liste de prix.

**List view:** coloured by status, with filters on status, supplier, port of arrival and expected arrival date.

**Reports (script reports, read-only):**
- **Suivi des dossiers d'importation:** one row per Dossier with status, the dates, goods value, estimated and actual costs, the gap %, and the days in transit.
- **Coût de revient par article:** the landed cost per unit per item and Dossier, over time.

**Not included:** a dashboard page and number cards. They belong to CHW-115, which can reuse these reports.

## Permissions

- **New role "Responsable Import":** create, read and write Dossiers; finalise costs; update prices.
- **Purchase Manager:** read and write.
- **Purchase User, Accounts User:** read.
- The role and the permissions are exported to fixtures.

## Rules and errors

- Money: `flt(value, precision)` with the field precision, and the company currency for MAD fields.
- Only one company per Dossier: every linked document must belong to it.
- A purchase order line can't be pulled into a second Dossier for more than its remaining quantity.
- "Finaliser" refuses with a clear message:
  - when a line isn't received;
  - when a cost line has neither an actual amount nor "keep the estimate";
  - when the split engine reports an error.
- No core edits:
  - the Custom Fields are fixtures;
  - doc_events on Purchase Receipt, Purchase Invoice and Landed Cost Voucher;
  - everything else lives in the new DocTypes.
- All user-facing text is in French, wrapped in `_()` / `__()`.

## Tests

All with fictional data, on a test company with the Standard chart and perpetual inventory.

**Split engine (unit tests):**
- each method;
- exact totals after rounding;
- the remainder going to the largest row;
- the error cases.

**Weight and volume fallback:** from the item card, and the "source" flag.

**Groupage flow (integration):**
- two suppliers, EUR and USD at different rates;
- pull the orders, create the receipts and submit them;
- three cost invoices (freight by weight, duties by value, bank fees by value), then finalise;
- check:
  - the voucher's `applicable_charges` per row;
  - the new valuation rate per item;
  - that the Dossier's actual costs equal the vouchers' total.

**Also:**
- a partial receipt;
- a late cost invoice that creates a second voucher for the difference only;
- cancelling a voucher, which brings the status back;
- the status changes;
- closing refused while a document is still a draft;
- the price suggestion, and the price list update for the ticked rows only.

**Sites:** `generic.localhost`, `marone.localhost` and CI. Reviews: `frappe-reviewer` and `accounting-checker`, since this touches stock valuation.

## Delivery

Three PRs, each usable on its own:

1. Dossier, Article lines, Custom Fields, Récupérer les commandes, Créer la réception, statuses.
2. Cost types, Frais lines, cost invoice tracking, the split engine, Finaliser les coûts (Landed Cost Voucher).
3. Pricing, the two reports, the role and permissions.

**Estimate:** 28 to 36 h. CHW-106's original 16 to 24 h didn't include the split engine or pricing.

## Out of scope

- Customs TVA and the TVA templates (CHW-102, parked).
- CGNC accounts (CHW-101, parked).
- Role dashboards (CHW-115).
- The forecast-vs-actual import report beyond the "Suivi" report (CHW-115).
- A Dossier for exports.
- Stock in transit warehouses.
