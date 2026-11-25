frappe.ui.form.on("Trade Import Settings", {
	refresh(frm) {
		if (!frm.doc.enable_stock_comptable) {
			return;
		}
		frm.add_custom_button(
			__("Initialiser depuis le stock réel"),
			() => {
				frappe.confirm(
					__(
						"Créer les soldes de stock comptable manquants à partir du stock réel ? Les articles qui ont déjà un solde ne sont pas modifiés."
					),
					() => {
						frappe.call({
							method: "trade_import.stock_comptable.initialise_from_real_stock",
							freeze: true,
							callback: () => {
								frappe.msgprint(
									__(
										"Initialisation lancée en arrière-plan. Un message vous prévient à la fin."
									)
								);
							},
						});
					}
				);
			},
			__("Actions")
		);
	},
});
