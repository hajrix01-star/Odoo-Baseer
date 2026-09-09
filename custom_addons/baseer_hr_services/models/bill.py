"""Protect the original service bill; native payment and reversal remain usable."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from .service import internal


class ServiceBill(models.Model):
    _inherit = 'account.move'

    baseer_hr_service_id = fields.Many2one('baseer.hr.service', readonly=True, copy=False, ondelete='restrict', index=True, check_company=True)
    _service_unique = models.Constraint('unique(baseer_hr_service_id)', 'An employee service can issue only one original supplier bill.')

    def _hr_service_protect(self):
        if not internal(self) and self.filtered('baseer_hr_service_id'):
            raise UserError(_('The employee-service bill is protected. Use a native reversal for a reviewed correction.'))

    @api.model_create_multi
    def create(self, vals_list):
        default = self.default_get(['baseer_hr_service_id']).get('baseer_hr_service_id')
        if not internal(self) and any(values.get('baseer_hr_service_id', default) for values in vals_list):
            raise AccessError(_('Service bill links can only be created by employee-service approval.'))
        return super().create(vals_list)

    def write(self, values):
        if not internal(self) and 'baseer_hr_service_id' in values:
            raise AccessError(_('The employee-service source link cannot be changed.'))
        if set(values) & {'partner_id', 'company_id', 'currency_id', 'journal_id', 'invoice_line_ids', 'line_ids',
                          'move_type', 'invoice_date', 'date', 'fiscal_position_id', 'invoice_payment_term_id',
                          'amount_total', 'amount_untaxed', 'amount_tax'} or values.get('state') == 'draft':
            self._hr_service_protect()
        return super().write(values)

    def button_draft(self):
        self._hr_service_protect()
        return super().button_draft()

    def unlink(self):
        if self.filtered('baseer_hr_service_id'):
            raise UserError(_('The original employee-service bill cannot be deleted.'))
        return super().unlink()


class ServiceBillLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        default = self.default_get(['move_id']).get('move_id')
        self.env['account.move'].browse([values.get('move_id', default) for values in vals_list if values.get('move_id', default)])._hr_service_protect()
        return super().create(vals_list)

    def write(self, values):
        if set(values) & {'move_id', 'partner_id', 'company_id', 'account_id', 'currency_id', 'product_id', 'quantity',
                          'price_unit', 'discount', 'debit', 'credit', 'balance', 'amount_currency', 'tax_ids', 'tax_line_id',
                          'price_subtotal', 'price_total', 'amount_residual', 'amount_residual_currency', 'display_type'}:
            (self.move_id | self.env['account.move'].browse(values.get('move_id')))._hr_service_protect()
        return super().write(values)

    def unlink(self):
        self.move_id._hr_service_protect()
        return super().unlink()
