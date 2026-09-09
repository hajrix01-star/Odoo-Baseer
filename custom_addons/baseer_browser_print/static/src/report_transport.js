import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { parse } from "@web/core/network/download";
import { ConnectionLostError, makeErrorFromResponse } from "@web/core/network/rpc";
import { getReportUrl } from "@web/webclient/actions/reports/utils";

/** Use the same authenticated POST and context as Odoo's download action. */
export async function prepareReport(action, userContext) {
    const controller = new AbortController();
    const timeout = browser.setTimeout(() => controller.abort(), 120000);
    try {
        const body = new FormData();
        body.append("data", JSON.stringify([getReportUrl(action, "pdf"), action.report_type]));
        body.append("context", JSON.stringify({ ...userContext, ...action.context }));
        body.append("token", "baseer-browser-print");
        if (odoo.csrf_token) {
            body.append("csrf_token", odoo.csrf_token);
        }
        const response = await browser.fetch("/report/download", {
            method: "POST", body, credentials: "same-origin", cache: "no-store",
            signal: controller.signal,
        }).catch((error) => {
            if (error.name === "AbortError") {
                throw error;
            }
            throw new ConnectionLostError("/report/download");
        });
        if (response.status === 502) {
            throw new ConnectionLostError("/report/download");
        }
        const blob = await response.blob();
        if (!response.ok || blob.type.split(";")[0] !== "application/pdf") {
            const doc = new DOMParser().parseFromString(await blob.text(), "text/html");
            const nodes = doc.body.children.length ? doc.body.children : [doc.body];
            let error;
            try {
                error = JSON.parse((nodes[1] || nodes[0]).textContent);
            } catch {
                throw new Error(_t("The PDF could not be prepared. Check your connection and access, then try again."));
            }
            throw makeErrorFromResponse(error);
        }
        if (await blob.slice(0, 5).text() !== "%PDF-") {
            throw new Error(_t("The server did not return a valid PDF report."));
        }
        let filename = `${action.name || "Report"}.pdf`;
        try {
            filename = parse((response.headers.get("Content-Disposition") || "").replace(/;$/, "")).parameters.filename || filename;
        } catch {
            // A missing filename does not invalidate an otherwise valid PDF.
        }
        return { blob, filename };
    } catch (error) {
        if (error.name === "AbortError") {
            throw new Error(_t("Preparing the report took too long. Try a smaller selection."));
        }
        throw error;
    } finally {
        browser.clearTimeout(timeout);
    }
}
