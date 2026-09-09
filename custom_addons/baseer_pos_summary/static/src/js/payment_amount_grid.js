/** @odoo-module **/

import { registry } from "@web/core/registry";
import { MonetaryField } from "@web/views/fields/monetary/monetary_field";
import { RadioField, radioField } from "@web/views/fields/radio/radio_field";
import { X2ManyField, x2ManyField } from "@web/views/fields/x2many/x2many_field";

// Only the presentation changes. Native child records and MonetaryField own
// parsing, dirty-state flushing, onchange and the parent's normal save cycle.
export class PaymentAmountGrid extends X2ManyField {
    static template = "baseer_pos_summary.PaymentAmountGrid";
    static components = { ...X2ManyField.components, MonetaryField };

    amountInputId(record) {
        return `${this.props.id || "baseer_payment_amount"}_${record.id}`;
    }
}

registry.category("fields").add("baseer_payment_amount_grid", {
    ...x2ManyField,
    component: PaymentAmountGrid,
    supportedTypes: ["one2many"],
});

// The full-day selection belongs to the schedule control. A split day selects
// one of its two shifts here; validation remains authoritative on the server.
export class SummaryShiftRadio extends RadioField {
    get items() {
        return super.items.filter(([value]) => value !== "all");
    }
}

registry.category("fields").add("baseer_summary_shift_radio", {
    ...radioField,
    component: SummaryShiftRadio,
    supportedTypes: ["selection"],
});
