from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    partners = env['res.partner'].with_context(active_test=False).search([
        ('baseer_name_ar', '=', False), ('baseer_name_en', '=', False),
        '|', ('is_company', '=', True), ('supplier_rank', '>', 0),
    ])
    partners._baseer_initialize_name_parts()
