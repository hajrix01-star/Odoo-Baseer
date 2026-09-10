import { Component, onMounted, onWillDestroy, useState } from "@odoo/owl";
import { Domain } from "@web/core/domain";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { KeepLast } from "@web/core/utils/concurrency";
import { useBus, useService } from "@web/core/utils/hooks";
import { ListRenderer } from "@web/views/list/list_renderer";
import { listView } from "@web/views/list/list_view";
import { KanbanRenderer } from "@web/views/kanban/kanban_renderer";
import { kanbanView } from "@web/views/kanban/kanban_view";

const FILTER_NAME = "baseer_financial_register_kpi";

export class FinancialRegisterKpis extends Component {
    static template = "baseer_financial_register.Kpis";
    static props = { list: Object };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, error: false, currencyGroups: [], asOf: "", selected: "", companyName: "", changingMode: false, actionError: false });
        this.keepLast = new KeepLast();
        this.requestVersion = 0;
        this.destroyed = false;
        this.scheduled = false;
        useBus(this.env.searchModel, "update", () => this.scheduleRefresh());
        // Native model reloads include returning from a source invoice/payment form.
        useBus(this.props.list.model.bus, "update", () => this.scheduleRefresh());
        onMounted(() => this.scheduleRefresh());
        onWillDestroy(() => {
            this.destroyed = true;
            this.requestVersion++;
        });
    }

    scheduleRefresh() {
        if (this.destroyed || this.scheduled) {
            return;
        }
        this.scheduled = true;
        // SearchModel and the relational model may notify within the same turn.
        // Coalesce that burst without delaying filter feedback or using a timer.
        Promise.resolve().then(() => {
            this.scheduled = false;
            if (!this.destroyed) {
                void this.refresh();
            }
        });
    }

    getOwnedFilters() {
        return this.env.searchModel.getSearchItems((item) => item.name === FILTER_NAME && item.isActive);
    }

    get cashMonth() {
        return this.env.searchModel.context.baseer_register_cash_month || "";
    }

    get isCashMode() {
        return Boolean(this.cashMonth);
    }

    async openMode(cash, month) {
        if (this.state.changingMode) {
            return;
        }
        this.state.changingMode = true;
        this.state.actionError = false;
        try {
            const method = cash ? "action_open_register_cash" : "action_open_financial_register";
            const context = { ...this.env.searchModel.context };
            if (!cash) {
                context.allowed_company_ids = user.activeCompanies.map((company) => company.id);
            }
            const action = await this.orm.call("account.move", method, cash && month ? [month] : [], {
                context,
            });
            if (!this.destroyed) {
                await this.action.doAction(action);
            }
        } catch {
            if (!this.destroyed) {
                this.state.actionError = true;
            }
        } finally {
            if (!this.destroyed) {
                this.state.changingMode = false;
            }
        }
    }

    async onMonthChange(event) {
        const month = event.target.value;
        if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month)) {
            event.target.value = this.cashMonth;
            return;
        }
        if (month !== this.cashMonth) {
            await this.openMode(true, month);
            if (!this.destroyed) {
                event.target.value = this.cashMonth;
            }
        }
    }

    async refresh() {
        const version = ++this.requestVersion;
        const searchModel = this.env.searchModel;
        // Both getters return independent snapshots of the native search state.
        const domain = searchModel.domain;
        const context = searchModel.context;
        const snapshot = JSON.stringify([domain, context]);
        this.state.selected = this.getOwnedFilters()[0]?.baseerCardKey || "";
        this.state.loading = true;
        this.state.error = false;
        this.state.currencyGroups = [];
        this.state.asOf = "";
        this.state.companyName = "";
        try {
            const result = await this.keepLast.add(this.orm.call(
                "account.move", this.isCashMode ? "baseer_financial_register_cash_kpis" : "baseer_financial_register_kpis", [domain], { context }
            ));
            if (this.destroyed || version !== this.requestVersion) {
                return;
            }
            if (snapshot !== JSON.stringify([searchModel.domain, searchModel.context])) {
                this.scheduleRefresh();
                return;
            }
            this.state.currencyGroups = result.currency_groups;
            this.state.asOf = result.as_of;
            this.state.companyName = result.company_name || "";
            this.state.loading = false;
        } catch {
            if (!this.destroyed && version === this.requestVersion) {
                this.state.currencyGroups = [];
                this.state.error = true;
                this.state.loading = false;
            }
        }
    }

    cardKey(currency, section, card) {
        return `${currency.currency_id}:${section.key}:${card.key}`;
    }

    cardLabel(currency, section, card) {
        return `${section.label} — ${card.label}: ${card.display}${card.is_count ? "" : ` ${currency.currency_name}`}`;
    }

    selectCard(currency, section, card) {
        if (this.state.loading) {
            return;
        }
        const key = this.cardKey(currency, section, card);
        const filters = this.getOwnedFilters();
        const wasSelected = filters.some((filter) => filter.baseerCardKey === key);
        const searchModel = this.env.searchModel;
        for (const groupId of new Set(filters.map((filter) => filter.groupId))) {
            searchModel.deactivateGroup(groupId);
        }
        if (!wasSelected) {
            searchModel.createNewFilters([{
                name: FILTER_NAME,
                baseerCardKey: key,
                description: `${section.label}: ${card.label} (${currency.currency_name})`,
                domain: new Domain(card.domain).toString(),
            }]);
        }
        this.state.selected = wasSelected ? "" : key;
        this.scheduleRefresh();
    }

    get loadingLabel() {
        return _t("Loading financial summary…");
    }
}

export class FinancialRegisterListRenderer extends ListRenderer {
    static template = "baseer_financial_register.ListRenderer";
    static components = { ...ListRenderer.components, FinancialRegisterKpis };
}

export class FinancialRegisterKanbanRenderer extends KanbanRenderer {
    static template = "baseer_financial_register.KanbanRenderer";
    static components = { ...KanbanRenderer.components, FinancialRegisterKpis };
}

registry.category("views").add("baseer_financial_register_list", {
    ...listView,
    Renderer: FinancialRegisterListRenderer,
});
registry.category("views").add("baseer_financial_register_kanban", {
    ...kanbanView,
    Renderer: FinancialRegisterKanbanRenderer,
});
