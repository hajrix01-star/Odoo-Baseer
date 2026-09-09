import { Component, onMounted, onWillDestroy, useRef, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { downloadFile } from "@web/core/network/download";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { hidePDFJSButtons } from "@web/core/utils/pdfjs";
import { downloadReport } from "@web/webclient/actions/reports/utils";
import { prepareReport } from "./report_transport";

export class BrowserPrintDialog extends Component {
    static template = "baseer_browser_print.Dialog";
    static components = { Dialog };
    static props = { blob: Object, filename: String, close: Function };

    setup() {
        this.frame = useRef("pdf");
        this.url = URL.createObjectURL(this.props.blob);
        this.state = useState({ ready: false, warning: "", compatible: false, src: this.url });
        this.destroyed = false;
        this.labels = {
            title: _t("Print report"), print: _t("Print"), download: _t("Download PDF"),
            close: _t("Close"), preview: _t("PDF preview"),
            compatible: _t("Alternative preview"),
            help: _t("If the print window does not open, use the PDF viewer's print button or download the PDF."),
        };
        onMounted(() => {
            hidePDFJSButtons(this.frame.el, { hideDownload: true });
            if (navigator.pdfViewerEnabled === false) {
                this.useCompatible();
                return;
            }
            this.timer = browser.setTimeout(() => {
                if (!this.state.ready) {
                    this.useCompatible();
                }
            }, 5000);
        });
        onWillDestroy(() => {
            this.destroyed = true;
            browser.clearTimeout(this.timer);
            browser.clearTimeout(this.loadTimer);
            this.frame.el?.removeAttribute("src");
            URL.revokeObjectURL(this.url);
        });
    }

    async onLoad() {
        if (this.destroyed) {
            return;
        }
        if (this.state.compatible) {
            if (this.compatibleLoading) {
                return;
            }
            browser.clearTimeout(this.loadTimer);
            const app = this.frame.el.contentWindow.PDFViewerApplication;
            if (!app) {
                this.state.warning = this.labels.help;
                return;
            }
            this.compatibleLoading = true;
            try {
                await app.initializedPromise;
                if (!this.destroyed) {
                    this.waitForPages(app, 0);
                }
            } catch {
                if (!this.destroyed) {
                    this.state.warning = this.labels.help;
                }
            }
            return;
        }
        if (this.state.ready) {
            return;
        }
        browser.clearTimeout(this.timer);
        this.state.ready = true;
        this.print();
    }

    useCompatible() {
        if (this.destroyed || this.state.compatible) {
            return;
        }
        browser.clearTimeout(this.timer);
        this.state.compatible = true;
        this.state.ready = false;
        this.state.warning = "";
        this.state.src = `/web/static/lib/pdfjs/web/viewer.html?file=${encodeURIComponent(this.url)}#zoom=page-fit`;
        this.loadTimer = browser.setTimeout(() => {
            if (!this.destroyed && !this.state.ready) {
                this.state.warning = this.labels.help;
            }
        }, 30000);
    }

    async waitForPages(app, attempts) {
        if (this.destroyed || this.state.ready) {
            return;
        }
        if (app.pdfDocument && app.pdfViewer?.pageViewsReady) {
            try {
                await app.pdfViewer.pagesPromise;
                if (!this.destroyed && app.pdfViewer.pageViewsReady) {
                    app.pdfSidebar?.close();
                    this.state.ready = true;
                    this.print();
                }
            } catch {
                if (!this.destroyed) {
                    this.state.warning = this.labels.help;
                }
            }
        } else if (attempts < 120) {
            this.timer = browser.setTimeout(() => this.waitForPages(app, attempts + 1), 250);
        } else {
            this.state.warning = this.labels.help;
        }
    }

    print() {
        if (!this.state.ready || this.destroyed) {
            return;
        }
        try {
            if (this.state.compatible && !this.frame.el.contentWindow.PDFViewerApplication?.pdfViewer?.pageViewsReady) {
                this.state.warning = this.labels.help;
                return;
            }
            this.frame.el.contentWindow.focus();
            this.frame.el.contentWindow.print();
        } catch {
            this.state.warning = this.labels.help;
        }
    }

    download() {
        downloadFile(this.props.blob, this.props.filename, "application/pdf");
    }
}

export async function browserPrintHandler(action, options, env) {
    if (action.report_type !== "qweb-pdf" || action.context?.baseer_download_pdf) {
        return false;
    }
    downloadReport.wkhtmltopdfStatusProm ||= rpc("/report/check_wkhtmltopdf");
    // Preserve Odoo's HTML fallback and upgrade warning before fetching anything.
    if (await downloadReport.wkhtmltopdfStatusProm !== "ok") {
        return false;
    }
    let report;
    env.services.ui.block();
    try {
        report = await prepareReport(action, user.context);
    } finally {
        env.services.ui.unblock();
    }
    await new Promise((resolve) => {
        env.services.dialog.add(BrowserPrintDialog, report, { onClose: resolve });
    });
    // Native action_service owns wizard closing and onClose callbacks.
    return true;
}

registry.category("ir.actions.report handlers").add("baseer_browser_print", browserPrintHandler);
