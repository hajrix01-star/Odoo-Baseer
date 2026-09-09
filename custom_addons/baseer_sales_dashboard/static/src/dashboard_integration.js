import { useExternalListener, useState } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { DashboardLoader } from "@spreadsheet_dashboard/bundle/dashboard_action/dashboard_loader_service";
import { SpreadsheetDashboardAction } from "@spreadsheet_dashboard/bundle/dashboard_action/dashboard_action";
import { DashboardDateFilter } from "@spreadsheet_dashboard/bundle/dashboard_action/dashboard_date_filter/dashboard_date_filter";
import { DateFilterDropdown } from "@spreadsheet/global_filters/components/date_filter_dropdown/date_filter_dropdown";
import { BaseerSalesDashboard } from "./sales_dashboard";

export function westernDateDigits(value) {
    return value.replace(/[\u0660-\u0669\u06f0-\u06f9]/g, (digit) =>
        String(digit.charCodeAt(0) - (digit >= "\u06f0" ? 0x06f0 : 0x0660)));
}

// Reuse the native filter and picker; this dashboard changes only digit display.
export class BaseerDateFilterDropdown extends DateFilterDropdown {
    static template = "baseer_sales_dashboard.DateFilterDropdown";
    getDescription(type) { return westernDateDigits(super.getDescription(type)); }
    dateFrom() { return super.dateFrom()?.reconfigure({ numberingSystem: "latn" }); }
    dateTo() { return super.dateTo()?.reconfigure({ numberingSystem: "latn" }); }
}

export class BaseerDashboardDateFilter extends DashboardDateFilter {
    static components = { ...DashboardDateFilter.components, DateFilterDropdown: BaseerDateFilterDropdown };
    get inputValue() { return westernDateDigits(super.inputValue); }
}

patch(DashboardLoader.prototype, {
    _getFetchGroupsSpecification() {
        const specification = super._getFetchGroupsSpecification(...arguments);
        specification.published_dashboard_ids.fields.baseer_dashboard_kind = {};
        return specification;
    },
});

patch(SpreadsheetDashboardAction, {
    components: { ...SpreadsheetDashboardAction.components, BaseerSalesDashboard, BaseerDashboardDateFilter },
});

patch(SpreadsheetDashboardAction.prototype, {
    setup() {
        // Register before the native spreadsheet print hook. This dashboard uses
        // the browser's page printing, never the empty compatibility workbook.
        useExternalListener(window, "keydown", (event) => {
            if (this.isBaseerSalesDashboard && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "p") {
                event.stopImmediatePropagation();
            }
        }, { capture: true });
        super.setup(...arguments);
        this.baseerDate = useState({ value: { type: "relative", period: "month_to_date" } });
    },

    updateBaseerDate(value) {
        // Undefined is the native component's explicit All time selection.
        this.baseerDate.value = value || null;
    },

    get isBaseerSalesDashboard() {
        return this.loader.getActiveDashboard()?.data.baseer_dashboard_kind === "sales_summary";
    },
});
