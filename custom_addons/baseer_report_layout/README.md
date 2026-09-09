# Baseer report layout

Presentation-only extension for ERP Heritage reports on Odoo 19 Community.
Installed for evaluation in `baseer_reports_qa_20260907` only.

- Profit and loss and cash flow only; their default two-column tables are centered at up to 820px.
- Comparison and analytic layouts retain the upstream multi-column scrolling behavior.
- Mobile basic filters use the existing More filters / Hide filters control.
- Existing row heights, financial values and backend methods are untouched.
- Since 19.0.1.1.0, Arabic PDF exports of these two reports embed IBM Plex Sans Arabic Regular/Bold. Screen fonts, English PDF and XLSX remain unchanged.
- No additional Python or JavaScript dependency; no copied vendor files.

Remove this module in QA to restore the vendor screen layout. Do not uninstall its dependency modules as part of that rollback.

The width token and scoped styles live in `static/src/report_layout.scss`;
`static/src/report_layout.xml` adds conditional classes to the existing OWL template.
Tested against eh_account_dynamic_reports 19.0.1.8.1 and eh_account_base 19.0.1.8.0.

PDF typography is inherited in `report/arabic_pdf_font.xml`, including a separate footer font declaration for wkhtmltopdf. Fonts are bundled locally under SIL OFL 1.1; see `static/fonts/SOURCE.md` and `LICENSE.txt`. No runtime CDN or system font installation is needed. Remove the three inherited/font templates and their manifest data entry (then upgrade this module) to revert PDF typography while keeping the screen layout.
