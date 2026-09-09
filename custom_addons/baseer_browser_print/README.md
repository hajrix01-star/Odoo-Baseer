# Baseer Browser Print — Odoo 19

Install this LGPL-3 addon to print native `qweb-pdf` report actions from a browser
preview. Print, Download PDF and Close reuse the same server-generated PDF.
The browser may require clicking Print again or using its PDF viewer toolbar.
Printing is interactive: this module does not send jobs silently or confirm paper output.

Native report authorization, company/language context, wizard options and error
handling remain in Odoo. No business models or accounting calculations are added.
Non-PDF actions and custom direct download routes remain unchanged. Integrations
can request the original download with `context: {baseer_download_pdf: true}`.

The preview object URL is released on close. Temporary browser/OS/printer data
may still exist; this is not a guarantee of secure erasure. Automatic printing
depends on browser PDF support; Download PDF remains available.

If the native PDF frame does not load within five seconds, the preview switches
to Odoo's bundled PDF.js viewer. Alternative preview also switches manually.
PDF.js printing rasterizes pages at its existing default 150 dpi; native PDF
printing is preferred for vector quality. Downloads always retain the original
PDF bytes. No bundled viewer source or global settings are modified.
