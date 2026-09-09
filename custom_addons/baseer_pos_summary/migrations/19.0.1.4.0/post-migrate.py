from odoo import api, SUPERUSER_ID
from odoo.addons.baseer_pos_summary.models.common import TOKEN_KEY, INTERNAL_TOKEN


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['pos.config']._repair_summary_service_products()
    for summary in env['baseer.pos.summary'].search([('state', '=', 'approved')]):
        scoped = summary.with_company(summary.company_id).with_context(**{TOKEN_KEY: INTERNAL_TOKEN})
        originals = scoped._native_moves()
        (originals | originals.reversal_move_ids).write({'baseer_pos_summary_id': summary.id})
