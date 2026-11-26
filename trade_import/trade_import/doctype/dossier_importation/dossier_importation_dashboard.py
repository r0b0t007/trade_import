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
