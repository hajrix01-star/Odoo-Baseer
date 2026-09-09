"""Suggest Expense for newly selected service leaves; no historical migration."""
from odoo import api, models
from .catalog import SERVICES


class PurchaseBatchLine(models.Model):
    _inherit = 'baseer.purchase.batch.line'

    def _baseer_is_seed_service(self):
        self.ensure_one()
        category = self.category_map_id.category_id
        # Only inspect trusted catalog identities after reading the caller's
        # mapping. Operators need no model-data administration privileges.
        return bool(category and self.env['ir.model.data'].sudo().search_count([
            ('module', '=', 'baseer_service_seed'), ('model', '=', 'product.category'),
            ('res_id', '=', category.id),
            ('name', 'in', [f'category_{service[0]}' for service in SERVICES]),
        ], limit=1))

    @api.onchange('category_map_id')
    def _onchange_baseer_service_category(self):
        for line in self:
            if line._baseer_is_seed_service():
                line.entry_type = 'expense'

    @api.onchange('partner_id')
    def _onchange_partner_default_category(self):
        result = super()._onchange_partner_default_category()
        self._onchange_baseer_service_category()
        return result

    @api.model_create_multi
    def create(self, vals_list):
        # The parent resolves supplier defaults and validates company ownership.
        # Only these just-created records may receive the missing type default.
        records = super().create(vals_list)
        for record, values in zip(records, vals_list):
            if 'entry_type' not in values and record._baseer_is_seed_service():
                record.entry_type = 'expense'
        return records
