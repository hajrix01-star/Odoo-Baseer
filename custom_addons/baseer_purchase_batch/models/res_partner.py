"""Native, company-dependent supplier category suggestion for batch entry."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    baseer_purchase_category_map_id = fields.Many2one(
        'baseer.purchase.category.map', string='Default Purchase Category',
        company_dependent=True, check_company=True, copy=False, ondelete='restrict',
        help='Suggested category for new purchase batch rows in the active company. You can change it on each row.',
    )

    def _check_purchase_category_configuration_rights(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(_('Only accounting managers can configure supplier default purchase categories.'))

    @api.constrains('baseer_purchase_category_map_id')
    def _check_purchase_category_company(self):
        for partner in self:
            mapping = partner.baseer_purchase_category_map_id
            if mapping:
                mapping.check_access('read')
                if mapping.company_id != self.env.company or not mapping.active:
                    raise ValidationError(_('The supplier default category must be active and belong to the active company.'))

    @api.model_create_multi
    def create(self, vals_list):
        key = 'baseer_purchase_category_map_id'
        has_context_default = 'default_' + key in self.env.context
        if any(key in values for values in vals_list) or has_context_default:
            self._check_purchase_category_configuration_rights()
        records = super().create(vals_list)
        # Also validate a native context default, which need not occur in vals.
        if has_context_default:
            records._check_purchase_category_company()
        return records

    def write(self, values):
        if 'baseer_purchase_category_map_id' in values:
            self._check_purchase_category_configuration_rights()
        return super().write(values)
