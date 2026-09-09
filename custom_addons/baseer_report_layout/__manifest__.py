{
    "name": "Baseer Report Layout",
    "summary": "Compact screen layout for profit and loss and cash flow",
    "version": "19.0.1.1.1",
    "author": "Baseer",
    "license": "LGPL-3",
    "depends": ["eh_account_dynamic_reports"],
    "data": ["report/arabic_pdf_font.xml"],
    "assets": {
        "web.assets_backend": [
            "baseer_report_layout/static/src/js/latin_date_field.js",
            "baseer_report_layout/static/src/report_layout.xml",
            "baseer_report_layout/static/src/report_layout.scss",
        ],
    },
    "installable": True,
    "application": False,
}
