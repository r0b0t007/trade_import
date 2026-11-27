frappe.ui.form.on("Dossier Importation", {
	refresh(frm) {
		if (frm.is_new()) return;
		const open_for_changes =
			frm.perm[0].write && !["Coûts finalisés", "Clôturé"].includes(frm.doc.status);
		if (!open_for_changes) return;
		const group = __("Actions");
		frm.add_custom_button(
			__("Récupérer les commandes"),
			() => save_first(frm, () => show_order_dialog(frm)),
			group
		);
		const to_receive = (frm.doc.articles || []).some((row) => row.qty > row.received_qty);
		if (to_receive) {
			frm.add_custom_button(
				__("Créer la réception"),
				() => save_first(frm, () => create_receipts(frm)),
				group
			);
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

// The server reads the saved Dossier: save pending edits first, then carry on.
function save_first(frm, next) {
	if (frm.is_dirty()) {
		frm.save().then(next);
	} else {
		next();
	}
}

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
						label: __("Lignes de commande"),
						cannot_add_rows: true,
						cannot_delete_rows: true,
						in_place_edit: false,
						data: lines,
						fields: [
							{
								fieldname: "purchase_order",
								fieldtype: "Link",
								options: "Purchase Order",
								label: __("Commande"),
								in_list_view: 1,
								read_only: 1,
							},
							{
								fieldname: "supplier",
								fieldtype: "Link",
								options: "Supplier",
								label: __("Fournisseur"),
								in_list_view: 1,
								read_only: 1,
							},
							{
								fieldname: "item_code",
								fieldtype: "Link",
								options: "Item",
								label: __("Article"),
								in_list_view: 1,
								read_only: 1,
							},
							{
								fieldname: "qty",
								fieldtype: "Float",
								label: __("Reste"),
								in_list_view: 1,
								read_only: 1,
							},
							{
								fieldname: "currency",
								fieldtype: "Link",
								options: "Currency",
								label: __("Devise"),
								in_list_view: 1,
								read_only: 1,
							},
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
						args: {
							dossier: frm.doc.name,
							po_items: chosen.map((row) => row.purchase_order_item),
						},
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
			frappe.msgprint(
				__("Réceptions créées en brouillon : {0}", [
					(message || []).map((name) => frappe.utils.escape_html(name)).join(", "),
				])
			);
			frm.reload_doc();
		},
	});
}
