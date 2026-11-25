frappe.query_reports["Stock vs Stock Comptable"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Société"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("company"),
		},
		{
			fieldname: "warehouse",
			label: __("Entrepôt"),
			fieldtype: "Link",
			options: "Warehouse",
			get_query: function () {
				const company = frappe.query_report.get_filter_value("company");
				return company ? { filters: { company } } : {};
			},
		},
		{
			fieldname: "item_code",
			label: __("Article"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "only_differences",
			label: __("Écarts uniquement"),
			fieldtype: "Check",
			default: 0,
		},
	],
};
