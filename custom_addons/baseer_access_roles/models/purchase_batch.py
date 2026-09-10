from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class PurchaseBatch(models.Model):
    _inherit = 'baseer.purchase.batch'

    def action_approve(self):
        # Block before the parent's already-approved shortcut as well as posting.
        if self.env.user.has_group('baseer_access_roles.group_cashier'):
            raise AccessError(_('Only the accountant or owner can approve purchase batches.'))
        return super().action_approve()

    def action_view_bills(self):
        if self.env.user.has_group('baseer_access_roles.group_cashier'):
            raise AccessError(_('Cashiers can view their batch details, not the accounting documents.'))
        return super().action_view_bills()

    def _get_print_data(self):
        if self.env.user.has_group('baseer_access_roles.group_cashier'):
            self.ensure_one()
            self.check_access('read')
            if self.company_id not in self.env.user.company_ids:
                raise AccessError(_('The company is outside your authorized companies.'))
            # Fixed-shape existing summary of the verified own batch only. Elevation
            # reads its bill reference, not native invoice/payment records for RPC.
            return super(PurchaseBatch, self.sudo())._get_print_data()
        return super()._get_print_data()


class PurchaseBatchLine(models.Model):
    _inherit = 'baseer.purchase.batch.line'

    baseer_bill_reference = fields.Char(
        string='Approved Bill Reference', compute='_compute_baseer_bill_reference',
        compute_sudo=False,
    )

    @api.depends('move_id')
    def _compute_baseer_bill_reference(self):
        for line in self:
            if line.move_id:
                line.check_access('read')
                line.baseer_bill_reference = line.move_id.sudo().name or ''
            else:
                line.baseer_bill_reference = ''

    def action_view_bill(self):
        if self.env.user.has_group('baseer_access_roles.group_cashier'):
            raise AccessError(_('Cashiers can view their batch details, not the accounting documents.'))
        return super().action_view_bill()
