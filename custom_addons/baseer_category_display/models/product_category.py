from odoo import api, models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    @api.depends('name', 'complete_name')
    @api.depends_context('hierarchical_naming')
    def _compute_display_name(self):
        if self.env.context.get('hierarchical_naming', False):
            return super()._compute_display_name()
        for category in self:
            category.display_name = category.name
