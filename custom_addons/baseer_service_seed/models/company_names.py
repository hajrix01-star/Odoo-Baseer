"""One bilingual identity: company fields project its native contact."""
from odoo import api, fields, models

from .res_partner import compose_names


class Company(models.Model):
    _inherit = 'res.company'

    baseer_name_ar = fields.Char(related='partner_id.baseer_name_ar', readonly=False)
    baseer_name_en = fields.Char(related='partner_id.baseer_name_en', readonly=False)

    @api.onchange('baseer_name_ar', 'baseer_name_en')
    def _onchange_baseer_names(self):
        for company in self:
            name = compose_names(company.baseer_name_ar, company.baseer_name_en)
            if name:
                company.name = name

    @api.model_create_multi
    def create(self, vals_list):
        prepared, components = [], []
        for values in vals_list:
            values = dict(values)
            parts = {key: values.pop(key) for key in ('baseer_name_ar', 'baseer_name_en') if key in values}
            if parts and not any(parts.values()) and values.get('name') and not values.get('partner_id'):
                parts = {}  # Native create forms may submit both optional fields blank.
            if parts:
                partner = self.env['res.partner'].browse(values.get('partner_id'))
                name = compose_names(parts.get('baseer_name_ar', partner.baseer_name_ar),
                                     parts.get('baseer_name_en', partner.baseer_name_en))
                if name:
                    values['name'] = name
            prepared.append(values)
            components.append(parts)
        companies = super().create(prepared)
        for company, parts in zip(companies, components):
            if parts:
                company.partner_id.write(parts)
            else:
                company.partner_id._baseer_initialize_name_parts()
        return companies

    def write(self, values):
        parts = {key: values[key] for key in ('baseer_name_ar', 'baseer_name_en') if key in values}
        if not parts:
            return super().write(values)
        self.check_access('write')
        # Related inverses run separately; batch to avoid intermediate company names.
        other = {key: value for key, value in values.items() if key not in parts and key != 'name'}
        result = super().write(other)
        self.mapped('partner_id').write(parts)
        return result
