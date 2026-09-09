{
    "name": "Baseer Cash Movement by Category",
    "summary": "Monthly cash receipts, categorized payments and VAT view",
    "version": "19.0.1.3.1",
    "author": "Baseer",
    "license": "LGPL-3",
    "depends": ["baseer_report_layout"],
    "data": ["data/report.xml", "report/report_note.xml"],
    "assets": {
        "web.assets_backend": [
            "baseer_cash_categories/static/src/cash_categories.js",
            "baseer_cash_categories/static/src/cash_categories.xml",
            "baseer_cash_categories/static/src/cash_categories.scss",
        ],
    },
    "installable": True,
    "application": False,
}
