import { Component, onWillStart, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";
import { clearUncommittedChanges } from "@web/webclient/actions/action_service";

export class BaseerLanguageMenu extends Component {
    static template = "baseer_web_navigation.LanguageMenu";
    static components = { Dropdown, DropdownItem };
    static props = {};

    setup() {
        this.orm = useService("orm");
        this.state = useState({ languages: [], busy: false });
        this.currentLanguage = user.lang;
        onWillStart(async () => {
            const installed = await this.orm.call("res.lang", "get_installed", []);
            const available = new Set(installed.map(([code]) => code));
            this.state.languages = [
                { code: "ar_001", label: "العربية" },
                { code: "en_US", label: "English" },
            ].filter(({ code }) => available.has(code));
        });
    }

    async selectLanguage(code) {
        if (this.state.busy || code === this.currentLanguage ||
            !this.state.languages.some((language) => language.code === code)) {
            return;
        }
        this.state.busy = true;
        try {
            // Use the same leave/save guard as native navigation. A failed save
            // or a canceled departure must not change the user's language.
            if (!(await clearUncommittedChanges(this.env))) {
                return;
            }
            await this.orm.write("res.users", [user.userId], { lang: code });
            browser.location.reload();
        } finally {
            this.state.busy = false;
        }
    }
}

registry.category("systray").add("baseer_web_navigation.LanguageMenu", {
    Component: BaseerLanguageMenu,
}, { sequence: 5 });
