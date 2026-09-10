import { Component, onWillStart, onWillUnmount, onWillUpdateProps, useRef, useState, useEffect } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { localization } from "@web/core/l10n/localization";

// Business values, periods, coverage and comparisons are all server-owned.
// Number conversion below is exclusively the canvas coordinate adapter.
export function chartCoordinate(value) {
    return value === null || value === undefined ? null : Number(value);
}

export class BaseerSalesDashboard extends Component {
    static template = "baseer_sales_dashboard.SalesDashboard";
    static props = { dashboardId: Number, dateFilter: { type: [Object, { value: null }], optional: true } };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            payload: null, loading: true, error: "", shiftMetric: "sales",
            shiftTab: "chart", paymentTab: "chart",
        });
        this.salesRef = useRef("salesChart");
        this.shiftRef = useRef("shiftChart");
        this.paymentRef = useRef("paymentChart");
        this.rootRef = useRef("dashboard");
        this.requestGeneration = 0;
        this.charts = [];
        this.disposed = false;
        // Mount the native loading state while the first read is in flight.
        onWillStart(() => { this.load(); });
        onWillUpdateProps((nextProps) => {
            if (nextProps.dateFilter !== this.props.dateFilter || nextProps.dashboardId !== this.props.dashboardId) {
                this.loadForProps(nextProps);
            }
        });
        useEffect(() => {
            this.drawCharts();
            return () => this.destroyCharts();
        }, () => [this.state.payload, this.state.shiftMetric, this.state.shiftTab, this.state.paymentTab]);
        onWillUnmount(() => {
            this.disposed = true;
            this.requestGeneration++;
            this.destroyCharts();
        });
    }

    get cards() {
        const values = this.state.payload?.cards || {};
        // Display-only examples for an empty dashboard; never part of the payload or calculations.
        const preview = { sales: "240,000", customers: "4,000", daily_sales: "8,000", daily_customers: "133", average_bill: "60" };
        return [
            { key: "sales", title: _t("Total sales"), note: _t("Tax included"), icon: "fa-line-chart" },
            { key: "customers", title: _t("Registered customers"), note: _t("From approved summaries"), icon: "fa-users" },
            { key: "daily_sales", title: _t("Average daily sales"), note: _t("Completed operating days"), icon: "fa-calendar" },
            { key: "daily_customers", title: _t("Average daily customers"), note: _t("Completed operating days"), icon: "fa-user" },
            { key: "average_bill", title: _t("Average bill"), note: _t("Per registered customer"), icon: "fa-credit-card" },
        ].map((spec) => ({
            ...spec, ...values[spec.key],
            display: this.hasRecordedData ? values[spec.key]?.display : preview[spec.key],
            currency: ["sales", "daily_sales", "average_bill"].includes(spec.key),
        }));
    }

    get periodLabel() {
        const filters = this.state.payload?.filters;
        return filters?.date_from && filters.date_to ? `${filters.date_from} — ${filters.date_to}` : "";
    }

    get comparisonLabel() {
        const period = this.state.payload?.comparison_period;
        return period?.date_from && period.date_to ? `${period.date_from} — ${period.date_to}` : "";
    }

    get granularityLabel() {
        const granularity = this.state.payload?.timeline?.granularity;
        return ["month", "monthly"].includes(granularity) ? _t("Monthly") : _t("Daily");
    }

    get points() { return this.state.payload?.timeline?.points || []; }
    get hasRecordedData() { return this.state.payload?.has_data === true; }
    get title() { return _t("Sales summaries"); }
    get combinedChartLabel() { return _t("Sales as bars and registered customers as a line over the selected period"); }
    get salesAxisLabel() { return `${_t("Sales")} (${this.state.payload?.company.currency || ""})`; }
    get customersAxisLabel() { return _t("Customers (count)"); }
    get labels() {
        return {
            subtitle: _t("Approved sales and registered customers"),
            refresh: _t("Refresh"), sources: _t("View approved summaries"),
            loading: _t("Loading the selected period…"), retry: _t("Try again"),
            comparison: _t("Compared with"), previous: _t("Previous"),
            sales: _t("Sales"), customers: _t("Customers"),
            chartTitle: _t("Sales and customers"),
            shifts: _t("Shift performance"), shift: _t("Shift"),
            metric: _t("Metric"), averageBill: _t("Average bill"),
            shiftEmpty: _t("No recorded shifts in this period"),
            shiftUnavailable: _t("This metric is unavailable for the recorded shifts"),
            payments: _t("Sales by payment method"), paymentMethod: _t("Payment method"),
            paymentShare: _t("Share of total sales (%)"),
            categorySales: _t("Sales by category"),
            paymentEmpty: _t("No payment allocations recorded in this period"),
            paymentIncomplete: _t("Some approved summaries have missing or inconsistent payment allocations. The breakdown is partial and percentages are unavailable."),
            applicationShare: _t("Applications share of total sales (%)"),
            coveredSales: _t("Covered sales"), uncoveredSales: _t("Sales excluded from payment breakdown"),
            coveredSummaries: _t("Covered summaries"), missingAllocations: _t("Missing allocations"),
            mismatchedAllocations: _t("Inconsistent allocations"),
            preview: _t("No data yet — illustrative preview"),
            unavailable: _t("Unavailable"), partial: _t("Partial period"),
            empty: _t("No approved sales summaries in this period"),
            emptyNote: _t("Approved summaries will appear here. Unrecorded and closed periods are shown as gaps."),
            completedDays: _t("Completed days"), partialDays: _t("Partial days"),
            closedDays: _t("Closed days"), missingDays: _t("Unrecorded days"),
            customerWarning: _t("Some approved sales have no registered customers. Affected averages are unavailable."),
            details: _t("Period details"), date: _t("Period"), status: _t("Coverage"),
        };
    }

    statusLabel(status) {
        return {
            complete: _t("Complete"), incomplete: _t("Partial"),
            closed: _t("Closed"), missing: _t("Not recorded"),
        }[status] || _t("Not recorded");
    }

    directionIcon(direction) {
        return { up: "fa-arrow-up", down: "fa-arrow-down", flat: "fa-minus", new: "fa-arrow-up", none: "fa-minus" }[direction] || "fa-minus";
    }

    directionLabel(direction) {
        return { up: _t("Increase"), down: _t("Decrease"), flat: _t("No change"), new: _t("New"), none: _t("Comparison unavailable") }[direction] || _t("Comparison unavailable");
    }

    load() { return this.loadForProps(this.props); }

    async loadForProps(props) {
        const generation = ++this.requestGeneration;
        const filters = { native: props.dateFilter ?? null };
        this.state.loading = true;
        this.state.error = "";
        this.state.payload = null;
        try {
            const payload = await this.orm.call("spreadsheet.dashboard", "get_baseer_sales_metrics", [
                [props.dashboardId], filters,
            ]);
            if (this.disposed || generation !== this.requestGeneration) { return; }
            this.state.payload = payload;
        } catch (error) {
            if (this.disposed || generation !== this.requestGeneration) { return; }
            const businessError = ["odoo.exceptions.UserError", "odoo.exceptions.ValidationError", "odoo.exceptions.AccessError"].includes(error?.data?.name);
            this.state.error = (businessError && error.data.message) || _t("The dashboard could not be loaded. Please try again.");
        } finally {
            if (!this.disposed && generation === this.requestGeneration) {
                this.state.loading = false;
            }
        }
    }

    get shiftRows() { return this.state.payload?.shift_performance?.rows || []; }
    get shiftMetricId() { return `baseer_sales_shift_metric_${this.props.dashboardId}`; }
    get shiftMetrics() {
        return [
            { key: "sales", label: this.salesAxisLabel, money: true },
            { key: "customers", label: this.customersAxisLabel, money: false },
            { key: "average_bill", label: `${_t("Average bill")} (${this.state.payload?.company.currency || ""})`, money: true },
        ];
    }
    get selectedShiftMetric() { return this.shiftMetrics.find((metric) => metric.key === this.state.shiftMetric); }
    get hasShiftData() { return this.shiftRows.some((row) => row.has_data); }
    get hasShiftMetric() { return this.shiftRows.some((row) => row.cards[this.state.shiftMetric]?.available); }

    onShiftMetricChange(event) {
        if (this.shiftMetrics.some((metric) => metric.key === event.target.value)) {
            this.state.shiftMetric = event.target.value;
        }
    }

    openShiftSources(row) {
        if (row.has_data && row.source_action) { return this.action.doAction(row.source_action); }
    }

    get paymentRows() { return this.state.payload?.payment_performance?.rows || []; }
    get paymentCoverage() { return this.state.payload?.payment_performance?.coverage; }
    get applicationShare() { return this.state.payload?.payment_performance?.application_share; }
    get categoryRows() { return this.state.payload?.payment_performance?.categories || []; }

    get tabs() { return [{ key: "chart", label: _t("Chart view") }, { key: "details", label: _t("Details") }]; }
    tabId(section, tab) { return `baseer_sales_${this.props.dashboardId}_${section}_${tab}`; }
    panelId(section) { return `baseer_sales_${this.props.dashboardId}_${section}_panel`; }

    selectTab(section, tab) {
        if (["shift", "payment"].includes(section) && ["chart", "details"].includes(tab)) {
            this.state[`${section}Tab`] = tab;
        }
    }

    onTabKeydown(event, section) {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { return; }
        event.preventDefault();
        const tab = event.key === "Home" ? "chart" : event.key === "End" ? "details" :
            this.state[`${section}Tab`] === "chart" ? "details" : "chart";
        this.selectTab(section, tab);
        event.currentTarget.parentElement.querySelector(`[data-view="${tab}"]`)?.focus();
    }

    openPaymentSources(row) {
        if (row.source_action) { return this.action.doAction(row.source_action); }
    }

    openSources() {
        const action = this.state.payload?.source_action;
        if (action) { return this.action.doAction(action); }
    }

    destroyCharts() {
        for (const chart of this.charts) { chart.destroy(); }
        this.charts = [];
    }

    drawCharts() {
        this.destroyCharts();
        if (!this.state.payload || !this.hasRecordedData || !this.salesRef.el || this.disposed) { return; }
        const font = getComputedStyle(this.rootRef.el).fontFamily;
        const rtl = localization.direction === "rtl";
        const points = this.points;
        const chart = new Chart(this.salesRef.el, {
                type: "bar",
                data: {
                    labels: points.map((point) => point.label),
                    datasets: [{
                        type: "bar", yAxisID: "sales", metric: "sales", order: 2,
                        label: this.salesAxisLabel,
                        data: points.map((point) => chartCoordinate(point.sales)),
                        borderColor: "#3385c4", backgroundColor: "#69afe5",
                        borderWidth: 0, borderRadius: 2, maxBarThickness: 42,
                        spanGaps: false,
                    }, {
                        type: "line", yAxisID: "customers", metric: "customers", order: 1,
                        label: this.customersAxisLabel,
                        data: points.map((point) => chartCoordinate(point.customers)),
                        borderColor: "#ba7428", backgroundColor: "#ba7428",
                        borderWidth: 2,
                        tension: 0,
                        fill: false,
                        spanGaps: false,
                        pointRadius: points.map((point) => point.status === "incomplete" ? 5 : 2),
                        pointStyle: points.map((point) => point.status === "incomplete" ? "triangle" : "circle"),
                        pointHoverRadius: 6,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    font: { family: font },
                    interaction: { mode: "index", intersect: false },
                    plugins: {
                        legend: {
                            display: true, position: "top", rtl, textDirection: rtl ? "rtl" : "ltr",
                            onClick: () => {},
                            labels: { boxWidth: 18, boxHeight: 8, padding: 18, font: { family: font, size: 12 } },
                        },
                        tooltip: {
                            rtl, textDirection: rtl ? "rtl" : "ltr",
                            titleFont: { family: font }, bodyFont: { family: font },
                            callbacks: {
                                label: (context) => `${context.dataset.label}: ${points[context.dataIndex][`${context.dataset.metric}_display`]}`,
                                afterLabel: (context) => this.statusLabel(points[context.dataIndex].status),
                            },
                        },
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 7, font: { family: font, size: 11 } },
                        },
                        sales: {
                            type: "linear", position: "left",
                            beginAtZero: true,
                            title: { display: true, text: this.salesAxisLabel, font: { family: font, size: 11 } },
                            grid: { color: "rgba(128,128,128,0.12)" },
                            ticks: {
                                callback: (value) => String(value),
                                font: { family: font, size: 11 },
                            },
                        },
                        customers: {
                            type: "linear", position: "right", beginAtZero: true,
                            title: { display: true, text: this.customersAxisLabel, font: { family: font, size: 11 } },
                            grid: { drawOnChartArea: false },
                            ticks: { precision: 0, callback: (value) => String(value), font: { family: font, size: 11 } },
                        },
                    },
                },
            });
            this.charts.push(chart);
        this.drawShiftChart(font, rtl);
        this.drawPaymentChart(font, rtl);
    }

    drawShiftChart(font, rtl) {
        if (this.state.shiftTab !== "chart" || !this.shiftRef.el || !this.hasShiftMetric) { return; }
        const rows = this.shiftRows;
        const metric = this.selectedShiftMetric;
        const color = metric.money ? "#69afe5" : "#ba7428";
        this.charts.push(new Chart(this.shiftRef.el, {
            type: "bar",
            data: {
                labels: rows.map((row) => row.label),
                datasets: [{
                    label: metric.label,
                    data: rows.map((row) => row.cards[metric.key].available ? chartCoordinate(row.cards[metric.key].value) : null),
                    backgroundColor: color, borderRadius: 2, maxBarThickness: 48,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                font: { family: font },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        rtl, textDirection: rtl ? "rtl" : "ltr",
                        titleFont: { family: font }, bodyFont: { family: font },
                        callbacks: { label: (context) => `${metric.label}: ${rows[context.dataIndex].cards[metric.key].display}` },
                    },
                },
                scales: {
                    x: { grid: { display: false }, ticks: { font: { family: font, size: 11 } } },
                    y: {
                        beginAtZero: true,
                        title: { display: true, text: metric.label, font: { family: font, size: 11 } },
                        grid: { color: "rgba(128,128,128,0.12)" },
                        ticks: { ...(metric.money ? {} : { precision: 0 }), callback: (value) => String(value), font: { family: font, size: 11 } },
                    },
                },
            },
        }));
    }

    drawPaymentChart(font, rtl) {
        if (this.state.paymentTab !== "chart" || !this.paymentRef.el || !this.paymentRows.length) { return; }
        const rows = this.paymentRows;
        this.charts.push(new Chart(this.paymentRef.el, {
            type: "bar",
            data: {
                labels: rows.map((row) => row.name),
                datasets: [{
                    label: this.salesAxisLabel,
                    data: rows.map((row) => row.sales.available ? chartCoordinate(row.sales.value) : null),
                    backgroundColor: "#69afe5", borderRadius: 2, maxBarThickness: 24,
                }],
            },
            options: {
                indexAxis: "y", responsive: true, maintainAspectRatio: false, animation: false,
                font: { family: font },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        rtl, textDirection: rtl ? "rtl" : "ltr",
                        titleFont: { family: font }, bodyFont: { family: font },
                        callbacks: { label: (context) => `${this.salesAxisLabel}: ${rows[context.dataIndex].sales.display}` },
                    },
                },
                scales: {
                    x: {
                        beginAtZero: true,
                        title: { display: true, text: this.salesAxisLabel, font: { family: font, size: 11 } },
                        grid: { color: "rgba(128,128,128,0.12)" },
                        ticks: { callback: (value) => String(value), font: { family: font, size: 11 } },
                    },
                    y: { grid: { display: false }, ticks: { font: { family: font, size: 11 } } },
                },
            },
        }));
    }
}
