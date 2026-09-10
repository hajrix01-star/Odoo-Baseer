/** @odoo-module **/
import { patch } from '@web/core/utils/patch';
import { Many2XAutocomplete } from '@web/views/fields/relational_utils';

// Keep the native matching, formatted labels, keyboard selection and RPC path.
patch(Many2XAutocomplete.prototype, {
    get baseerPartnerPriority() {
        const context = this.props.context || {};
        return this.props.resModel === 'res.partner' &&
            (context.baseer_partner_priority || ['supplier', 'customer'].includes(context.res_partner_search_mode));
    },
    get searchSpecification() {
        const specification = super.searchSpecification;
        return this.baseerPartnerPriority
            ? { ...specification, baseer_is_favorite: {} }
            : specification;
    },
});
