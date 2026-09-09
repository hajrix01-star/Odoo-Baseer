/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class PayrollMonthField extends Component {
    static template = "baseer_payroll.PayrollMonthField";
    static props = { ...standardFieldProps };

    setup() {
        this.input = useInputField({
            getValue: () => this.value ? this.value.toISODate().slice(0, 7) : "",
            parse: (value) => {
                if (this.input.el.validity.badInput || (value && !/^[0-9]{4}-(0[1-9]|1[0-2])$/.test(value))) {
                    throw new Error(_t("Choose a valid month and year."));
                }
                if (!value) {
                    return false;
                }
                const month = luxon.DateTime.fromISO(`${value}-01`);
                if (!month.isValid || month.year < 1 || month.year > 9999) {
                    throw new Error(_t("Choose a valid month and year."));
                }
                return month;
            },
        });
    }

    get value() {
        return this.props.record.data[this.props.name];
    }

    get formattedValue() {
        return this.value ? this.value.reconfigure({ numberingSystem: "latn", outputCalendar: "gregory" }).toFormat("LLLL yyyy") : "";
    }
}

registry.category("fields").add("baseer_payroll_month", {
    component: PayrollMonthField,
    displayName: _t("Payroll Month"),
    supportedTypes: ["date"],
});
