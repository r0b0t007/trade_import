frappe.listview_settings["Dossier Importation"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = {
			Brouillon: "gray",
			Commandé: "blue",
			"En transit": "orange",
			"En dédouanement": "yellow",
			Réceptionné: "green",
			"Coûts finalisés": "purple",
			Clôturé: "darkgrey",
		};
		return [__(doc.status), colors[doc.status] || "gray", "status,=," + doc.status];
	},
};
