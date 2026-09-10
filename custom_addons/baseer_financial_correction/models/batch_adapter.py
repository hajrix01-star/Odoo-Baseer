"""Keep one approved input row consistent with its corrected native documents."""
from odoo import _, api, Command, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.baseer_purchase_batch.models.purchase_batch import (
    BaseerPurchaseBatchLine, MAX_REFERENCE_CANDIDATES, checked_gross,
    monetary, native_quote, normalized_reference,
)

from .dispatcher import require_access


class CorrectionBatchAdapter(models.Model):
    _inherit = 'baseer.purchase.batch.line'

    baseer_cancelled = fields.Boolean(readonly=True, copy=False, default=False, index=True)
    baseer_cancel_reason = fields.Text(readonly=True, copy=False)
    baseer_cancelled_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    baseer_cancelled_at = fields.Datetime(readonly=True, copy=False)
    _CANCEL_FIELDS = {'baseer_cancelled', 'baseer_cancel_reason', 'baseer_cancelled_by_id', 'baseer_cancelled_at'}

    @api.model_create_multi
    def create(self, vals_list):
        if any(self._CANCEL_FIELDS.intersection(vals) for vals in vals_list) or any(
                'default_' + name in self.env.context for name in self._CANCEL_FIELDS):
            raise AccessError(_('Purchase cancellation history is controlled by the operation workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        if self._CANCEL_FIELDS.intersection(vals):
            raise AccessError(_('Purchase cancellation history is controlled by the operation workflow.'))
        return super().write(vals)

    def action_baseer_cancel_operation(self):
        self.ensure_one()
        require_access(self)
        if self.baseer_cancelled:
            raise UserError(_('This purchase row has already been cancelled.'))
        if self.batch_id.state != 'approved' or not self.move_id:
            raise UserError(_('Only an approved purchase row can be cancelled here.'))
        return self.env['baseer.financial.correction']._open_for_source(
            move=self.move_id, payment=self.payment_id, batch_line=self, operation='cancel')

    def _baseer_apply_invoice_cancellation(self, wizard, capability):
        from .correction import CORRECTION_CAPABILITY
        self.ensure_one()
        if capability is not CORRECTION_CAPABILITY:
            raise AccessError(_('Purchase cancellation history is controlled by the operation workflow.'))
        require_access(self)
        if (wizard.batch_line_id != self or wizard.create_uid != self.env.user
                or wizard.company_id != self.company_id or wizard.move_id != self.move_id
                or wizard.payment_id != self.payment_id or self.batch_id.state != 'approved'
                or self.move_id.state != 'cancel' or (self.payment_id and (
                    self.payment_id.state != 'canceled' or self.payment_id.move_id.state != 'cancel'))):
            raise ValidationError(_('The cancelled purchase row must match its cancelled invoice and payment.'))
        self.batch_id._lock_batches()
        super(BaseerPurchaseBatchLine, self).write({
            'baseer_cancelled': True, 'baseer_cancel_reason': wizard.reason.strip(),
            'baseer_cancelled_by_id': self.env.uid, 'baseer_cancelled_at': fields.Datetime.now()})

    def _baseer_check_correction_source(self, wizard):
        self.ensure_one()
        if self.baseer_cancelled:
            raise UserError(_('A cancelled purchase row cannot be edited.'))
        wizard.ensure_one()
        require_access(self)
        wizard.check_access('read')
        if (wizard.create_uid != self.env.user or wizard.batch_line_id != self
                or wizard.company_id != self.company_id or self.batch_id.state != 'approved'
                or wizard.move_id != self.move_id or wizard.payment_id != self.payment_id
                or self.move_id.move_type != 'in_invoice'):
            raise AccessError(_('The correction must belong to this approved row and its original documents.'))
        self.move_id._baseer_assert_correction_eligible()
        lines = self.move_id.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        account = self.category_map_id._validated_expense_account()
        if (len(lines) != 1 or lines.quantity != 1 or lines.discount != 0
                or lines.product_id != self.category_map_id.product_id or lines.account_id != account
                or lines.tax_ids != self.tax_id):
            raise UserError(_('The batch invoice no longer matches its original single-line category and tax. Use the native source review.'))
        return lines

    def _baseer_prepare_correction_values(self, wizard):
        line = self._baseer_check_correction_source(wizard)
        gross = checked_gross(wizard.gross_amount_input, self.env)
        partner = wizard.partner_id
        partner.check_access('read')
        if (not partner.active or any(record.company_id and record.company_id != self.company_id
                                     for record in partner | partner.commercial_partner_id)):
            raise ValidationError(_('Choose an active supplier accessible to the batch company.'))
        quote = native_quote(gross, self.tax_id, self.category_map_id.product_id, partner, self.company_id)
        # Keep the reviewed native batch reference rule, excluding this same bill.
        reference = normalized_reference(self.supplier_ref, self.env)
        candidates = self.env['account.move'].search([
            ('id', '!=', self.move_id.id), ('company_id', '=', self.company_id.id),
            ('commercial_partner_id', '=', partner.commercial_partner_id.id),
            ('move_type', 'in', ('in_invoice', 'in_refund')), ('state', 'in', ('draft', 'posted')),
        ], limit=MAX_REFERENCE_CANDIDATES + 1)
        if len(candidates) > MAX_REFERENCE_CANDIDATES:
            raise UserError(_('Supplier history exceeds the batch duplicate-check limit. Use native invoicing for this batch.'))
        if any(move.ref and move.ref.strip().casefold() == reference for move in candidates):
            raise ValidationError(_('A supplier document already uses this reference in this company.'))
        return {'partner_id': partner.id, 'invoice_line_ids': [Command.update(line.id, {
            'price_unit': float(quote['unit_price']),
        })]}

    def _baseer_apply_invoice_correction(self, wizard, capability):
        from .correction import CORRECTION_CAPABILITY
        if capability is not CORRECTION_CAPABILITY:
            raise AccessError(_('Approved purchase rows can only be updated by the reviewed correction.'))
        self._baseer_check_correction_source(wizard)
        self.batch_id._lock_batches()
        gross = checked_gross(wizard.gross_amount_input, self.env)
        bill, payment = self.move_id, self.payment_id
        quote = native_quote(gross, self.tax_id, self.category_map_id.product_id,
                             wizard.partner_id, self.company_id)
        if (bill.state != 'posted' or bill.partner_id != wizard.partner_id
                or bill.invoice_date != self.invoice_date or bill.date != self.invoice_date
                or monetary(bill.amount_total) != quote['gross']
                or monetary(bill.amount_untaxed) != quote['net'] or monetary(bill.amount_tax) != quote['tax']):
            raise ValidationError(_('The corrected native invoice does not match the purchase row.'))
        values = {'partner_id': bill.partner_id.id, 'gross_amount': float(gross)}
        if payment:
            payment.check_access('read')
            if payment.partner_id.commercial_partner_id != bill.partner_id.commercial_partner_id:
                raise ValidationError(_('The corrected payment and purchase row must have the same supplier.'))
            values.update(payment_method_line_id=payment.payment_method_line_id.id, is_credit=False)
        else:
            values.update(payment_method_line_id=False, is_credit=True)
        # The existing approval uses this same lexical superclass to write
        # server-owned values. Capability + source checks keep public write guards
        # intact; only this row changes and the surrounding transaction records
        # its immutable before/after audit. Do not reopen or reapprove the batch.
        super(BaseerPurchaseBatchLine, self).write(values)
        self._validate_inputs(strict_supplier=True)
        if (monetary(self.net_amount) != monetary(bill.amount_untaxed)
                or monetary(self.tax_amount) != monetary(bill.amount_tax)):
            raise ValidationError(_('The corrected purchase row totals differ from the native invoice.'))
        return True


class CorrectionBatchTotals(models.Model):
    _inherit = 'baseer.purchase.batch'

    @api.depends('line_ids.gross_amount', 'line_ids.net_amount', 'line_ids.tax_amount', 'line_ids.baseer_cancelled')
    def _compute_amounts(self):
        from decimal import Decimal
        for batch in self:
            active = batch.line_ids.filtered(lambda row: not row.baseer_cancelled)
            batch.amount_gross = float(sum((monetary(row.gross_amount) for row in active), Decimal('0')))
            batch.amount_net = float(sum((monetary(row.net_amount) for row in active), Decimal('0')))
            batch.amount_tax = float(sum((monetary(row.tax_amount) for row in active), Decimal('0')))

    def _get_print_data(self):
        data = super()._get_print_data()
        for line, row in zip(self.line_ids.sorted(lambda item: (item.sequence, item.id)), data['rows']):
            if line.baseer_cancelled:
                row['description'] = _('Cancelled. Original amount: %s', row['gross'])
                row['payment'] = _('Cancelled')
                row.update(gross='0.00', net='0.00', tax='0.00')
        return data
