/** @odoo-module **/

import { onWillRender } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { DateTimeField, dateField, dateTimeField } from "@web/views/fields/datetime/datetime_field";

function latinDigits(value) {
    if (Array.isArray(value)) {
        return value.map(latinDigits);
    }
    return value && value.numberingSystem !== "latn"
        ? value.reconfigure({ numberingSystem: "latn" })
        : value;
}

// Keep native dates and picker behavior; only this widget's presentation uses 0–9.
export class BaseerLatinDateField extends DateTimeField {
    setup() {
        super.setup();
        onWillRender(() => {
            const value = this.state.value;
            if (value && !Array.isArray(value) && value.numberingSystem !== "latn") {
                this.state.value = latinDigits(value);
            }
        });
    }

    getRecordValue() {
        return latinDigits(super.getRecordValue());
    }
}

registry.category("fields").add("baseer_latin_date", {
    ...dateField,
    component: BaseerLatinDateField,
});

registry.category("fields").add("baseer_latin_datetime", {
    ...dateTimeField,
    component: BaseerLatinDateField,
});
