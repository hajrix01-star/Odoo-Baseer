/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useSetupAction } from "@web/search/action_hook";
import { _t } from "@web/core/l10n/translation";
import { EhDynamicReportViewer } from "@eh_account_dynamic_reports/components/dynamic_report/dynamic_report";
import { formatCurrency } from "@eh_account_dynamic_reports/components/dynamic_report/report_format";

patch(EhDynamicReportViewer.prototype, {
    setup() {
        super.setup(...arguments);
        if (this.reportCode === "baseer_cash_categories") {
            this.state.options.baseer_include_tax = true;
            this.state.options.baseer_months = [this.state.options.date.date_from.slice(0, 7)];
            this.state.baseerMonthDraft = this.state.options.baseer_months[0];
            this.state.baseerMonthError = "";
            const restored = this.props.state?.baseerCashOptions;
            if (restored) {
                this.state.options = { ...this.state.options, ...restored };
                this._baseerRestoredOptions = restored;
            }
            useSetupAction({
                getLocalState: () => ({
                    baseerCashOptions: JSON.parse(JSON.stringify(this.state.options)),
                }),
            });
        }
    },
    savedViewOptionDefaults() {
        const defaults = super.savedViewOptionDefaults(...arguments);
        return this.reportCode === "baseer_cash_categories"
            ? { ...defaults, baseer_include_tax: true, baseer_months: [] } : defaults;
    },
    async refresh() {
        if (this.reportCode === "baseer_cash_categories" && this._baseerRestoredOptions) {
            this.state.options = { ...this.state.options, ...this._baseerRestoredOptions };
            this._baseerRestoredOptions = null;
        }
        if (this.reportCode === "baseer_cash_categories") this._baseerNormalizeSelection();
        return super.refresh(...arguments);
    },
    _baseerNormalizeSelection() {
        const options = this.state.options;
        const choices = this.state.choices.companies;
        const selected = (options.company_ids || []).find((id) => choices.some((company) => company.id === id))
            || choices[0]?.id;
        if (selected) {
            options.company_ids = [selected];
            options.primary_company_id = selected;
        }
        const valid = /^\d{4}-(0[1-9]|1[0-2])$/;
        let months = Array.isArray(options.baseer_months)
            ? [...new Set(options.baseer_months.filter((month) => typeof month === "string" && valid.test(month)))].sort() : [];
        if (!months.length) months = [options.date.date_from.slice(0, 7)];
        options.baseer_months = months;
        const last = months[months.length - 1];
        const [year, month] = last.split("-").map(Number);
        const lastDay = new Date(year, month, 0).getDate();
        options.date = { mode: "range", date_from: `${months[0]}-01`, date_to: `${last}-${lastDay}` };
        delete options.baseer_drill_column;
    },
    async onBaseerCompanyChange(companyId) {
        if (this.state.loading || this.state.options.company_ids[0] === companyId) return;
        this.state.options.company_ids = [companyId];
        this.state.options.primary_company_id = companyId;
        await this.refresh();
    },
    onBaseerMonthDraft(event) {
        this.state.baseerMonthDraft = event.target.value;
        this.state.baseerMonthError = "";
    },
    async onBaseerAddMonth() {
        const month = this.state.baseerMonthDraft;
        if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
            this.state.baseerMonthError = _t("Choose a valid month.");
            return;
        }
        if (this.state.options.baseer_months.includes(month)) return;
        if (this.state.options.baseer_months.length >= 12) {
            this.state.baseerMonthError = _t("Select no more than 12 months.");
            return;
        }
        const selection = [...this.state.options.baseer_months, month].sort();
        const ordinal = (item) => Number(item.slice(0, 4)) * 12 + Number(item.slice(5));
        if (ordinal(selection[selection.length - 1]) - ordinal(selection[0]) >= 24) {
            this.state.baseerMonthError = _t("Selected months must fit within a 24-month span.");
            return;
        }
        this.state.options.baseer_months = selection;
        this.state.baseerMonthError = "";
        await this.refresh();
    },
    async onBaseerRemoveMonth(month) {
        if (this.state.loading || this.state.options.baseer_months.length <= 1) return;
        this.state.options.baseer_months = this.state.options.baseer_months.filter((selected) => selected !== month);
        this.state.baseerMonthError = "";
        await this.refresh();
    },
    baseerMonthLabel(month) {
        const language = this.user?.context?.lang || "en_US";
        const locale = language.startsWith("ar") ? "ar-SA-u-ca-gregory-nu-latn" : language.replace("_", "-");
        const [year, number] = month.split("-").map(Number);
        return new Intl.DateTimeFormat(locale, { month: "long", year: "numeric", timeZone: "UTC" })
            .format(new Date(Date.UTC(year, number - 1, 1)));
    },
    baseerRemoveMonthLabel(month) {
        return _t("Remove month: %s", this.baseerMonthLabel(month));
    },
    _computeWindow() {
        if (this.reportCode !== "baseer_cash_categories") return super._computeWindow(...arguments);
        const lines = this.visibleLines();
        return { lines, total: lines.length, startIndex: 0, topPad: 0, bottomPad: 0 };
    },
    unsupportedOptions() {
        const options = super.unsupportedOptions(...arguments);
        return this.reportCode === "baseer_cash_categories"
            ? [...new Set([...options, "journal_ids", "partner_ids", "account_ids",
                "account_type_ids", "analytic_account_ids", "analytic_plan_ids",
                "presentation_currency_id", "show_zero"])] : options;
    },
    async onBaseerTaxChange(event) {
        this.state.options.baseer_include_tax = event.target.checked;
        await this.refresh();
    },
    nameStyle(line) {
        const style = super.nameStyle(...arguments);
        return this.reportCode === "baseer_cash_categories"
            ? style.replace("padding-left:", "padding-inline-start:") : style;
    },
    isDrillableLine(line) {
        return this.reportCode === "baseer_cash_categories"
            ? Boolean(line?.meta?.baseer_source_drilldown)
            : super.isDrillableLine(...arguments);
    },
    isDrillableColumn(line, column) {
        if (this.reportCode !== "baseer_cash_categories") return super.isDrillableColumn(...arguments);
        const index = this.valueColumnDefs().findIndex((candidate) => candidate.expression_label === column.expression_label);
        return this.isDrillableLine(line) && index >= 0 && typeof line.columns?.[index]?.value === "number"
            && Number.isFinite(line.columns[index].value);
    },
    onAmountCellClick(line, column, valueIndex = null) {
        if (this.reportCode !== "baseer_cash_categories") return super.onAmountCellClick(...arguments);
        if (!this.isDrillableColumn(line, column)) return;
        return this.onLineClick(line, { ...this.state.options, baseer_drill_column: column.expression_label });
    },
    formatLineValue(line, valueIndex) {
        if (this.reportCode === "baseer_cash_categories") {
            const column = this.valueColumnDefs()[valueIndex];
            const cell = line.columns?.[valueIndex];
            if (column?.figure_type === "baseer_percentage") return cell?.display_value || "";
            if (column?.figure_type === "monetary") {
                return formatCurrency(cell?.value, { ...this.state.payload.currency, symbol: "" },
                    "monetary", "en-US");
            }
        }
        return super.formatLineValue(...arguments);
    },
    cellClass(line, valueIndex) {
        const classes = super.cellClass(...arguments);
        return this.reportCode === "baseer_cash_categories"
            && this.valueColumnDefs()[valueIndex]?.figure_type === "baseer_percentage"
            ? `${classes} text-end eh_dr_num baseer_cash_percentage` : classes;
    },
    rowClass(line) {
        const classes = super.rowClass(...arguments);
        if (this.reportCode !== "baseer_cash_categories") return classes;
        const role = line.meta?.baseer_role;
        return `${classes} ${role ? `baseer_cash_${role}` : ""}`;
    },
});
