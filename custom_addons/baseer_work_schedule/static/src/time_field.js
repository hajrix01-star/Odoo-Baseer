import { Component, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { usePopover } from "@web/core/popover/popover_hook";
import { useAutofocus } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

let nextPickerId = 0;
const twoDigits = (value) => String(value).padStart(2, "0");

// Stage raw HH:MM locally. Duration, overlap and payroll stay server-owned.
export class ScheduleTimePopover extends Component {
    static template = "baseer_work_schedule.TimePopover";
    static props = { value: String, allowEndOfDay: Boolean, onApply: Function, close: Function };

    setup() {
        this.pickerId = `baseer_time_${nextPickerId++}`;
        const match = this.props.value.match(/^(\d{1,2}):(\d{2})$/);
        const hour = match ? Number(match[1]) : 8;
        const minute = match ? Number(match[2]) : 0;
        const valid = hour <= (this.props.allowEndOfDay ? 24 : 23) && minute < 60 &&
            (hour !== 24 || minute === 0);
        this.state = useState({ hour: valid ? twoDigits(hour) : "08", minute: valid ? twoDigits(minute) : "00", busy: false });
        this.hoursRef = useAutofocus({ refName: "hours", mobile: true });
        this.minutesRef = useRef("minutes");
        useEffect(() => this.scrollSelection(), () => [this.state.hour, this.state.minute]);
    }

    get hours() {
        return Array.from({ length: this.props.allowEndOfDay ? 25 : 24 }, (_, n) => twoDigits(n));
    }
    get minutes() {
        return Array.from({ length: this.state.hour === "24" ? 1 : 60 }, (_, n) => twoDigits(n));
    }
    get value() { return `${this.state.hour}:${this.state.minute}`; }
    get chooseTimeLabel() { return _t("Choose time"); }

    select(part, value) {
        if (this.state.busy) { return; }
        const choices = part === "hour" ? this.hours : this.minutes;
        if (!choices.includes(value)) { return; }
        this.state[part] = value;
        if (this.state.hour === "24") { this.state.minute = "00"; }
    }

    onOptionClick(ev) {
        const { part, value } = ev.currentTarget.dataset;
        this.select(part, value);
        (part === "hour" ? this.hoursRef : this.minutesRef).el.focus({ preventScroll: true });
    }

    onColumnKeydown(ev) {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(ev.key)) { return; }
        ev.preventDefault();
        ev.stopPropagation();
        const part = ev.currentTarget.dataset.part;
        const choices = part === "hour" ? this.hours : this.minutes;
        let index = choices.indexOf(this.state[part]);
        if (ev.key === "Home") { index = 0; }
        else if (ev.key === "End") { index = choices.length - 1; }
        else { index = Math.max(0, Math.min(choices.length - 1, index + (ev.key === "ArrowDown" ? 1 : -1))); }
        this.select(part, choices[index]);
    }

    scrollSelection() {
        for (const ref of [this.hoursRef, this.minutesRef]) {
            const column = ref.el;
            const selected = column?.querySelector('[aria-selected="true"]');
            if (selected) {
                // Scroll only the bounded column, never the modal or page.
                column.scrollTop = Math.max(0, selected.offsetTop - (column.clientHeight - selected.offsetHeight) / 2);
            }
        }
    }

    cancel() { if (!this.state.busy) { this.props.close(); } }

    onPanelKeydown(ev) {
        if (ev.key === "Escape") {
            ev.preventDefault();
            ev.stopPropagation();
            this.cancel();
        }
    }

    async apply() {
        if (this.state.busy) { return; }
        this.state.busy = true;
        try {
            await this.props.onApply(this.value);
            this.props.close();
        } finally {
            this.state.busy = false;
        }
    }
}

export class ScheduleTimeField extends Component {
    static template = "baseer_work_schedule.TimeField";
    static props = { ...standardFieldProps, allowEndOfDay: { type: Boolean, optional: true } };
    static defaultProps = { allowEndOfDay: false };

    setup() {
        this.state = useState({ busy: false, open: false });
        this.triggerRef = useRef("trigger");
        this.restoreFocus = false;
        useEffect(() => {
            if (this.restoreFocus && !this.state.open && !this.state.busy) {
                this.triggerRef.el?.focus({ preventScroll: true });
                this.restoreFocus = false;
            }
        }, () => [this.state.open, this.state.busy]);
        this.popover = usePopover(ScheduleTimePopover, {
            position: "bottom-start",
            popoverClass: "o_baseer_time_popover",
            role: "dialog",
            animation: false,
            onPositioned: (element) => element.setAttribute("aria-label", _t("Choose time")),
            onClose: () => {
                this.restoreFocus = true;
                this.state.open = false;
            },
        });
    }

    get value() { return this.props.record.data[this.props.name] || ""; }
    get triggerLabel() { return _t("Choose time: %s", this.value || "--:--"); }

    openPicker() {
        if (this.state.busy || this.props.readonly) { return; }
        if (this.popover.isOpen) { this.popover.close(); return; }
        this.state.open = true;
        this.popover.open(this.triggerRef.el, {
            value: this.value,
            allowEndOfDay: this.props.allowEndOfDay,
            onApply: (value) => this.applyValue(value),
            close: () => this.popover.close(),
        });
    }

    async applyValue(value) {
        if (this.state.busy) { return; }
        this.state.busy = true;
        try {
            await this.props.record.update({ [this.props.name]: value });
        } finally {
            this.state.busy = false;
        }
    }
}

registry.category("fields").add("baseer_schedule_time", {
    component: ScheduleTimeField,
    supportedTypes: ["char"],
    extractProps: ({ options }) => ({ allowEndOfDay: Boolean(options.allow_end_of_day) }),
});
