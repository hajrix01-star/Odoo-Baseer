"""Small hooks for dedicated summaries; ordinary native POS stays unchanged."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.point_of_sale.models.pos_order import PosOrder as NativePosOrder

from .common import internal


class PosSession(models.Model):
    _inherit = 'pos.session'

    baseer_summary_id = fields.Many2one('baseer.pos.summary', readonly=True, copy=False, ondelete='restrict', index=True)
    _baseer_summary_unique = models.Constraint('unique(baseer_summary_id)', 'A summary may own only one POS session.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            config = self.env['pos.config'].browse(vals.get('config_id') or self.env.context.get('default_config_id'))
            if config.baseer_summary_only or 'baseer_summary_id' in vals or 'default_baseer_summary_id' in self.env.context:
                if not internal(self) or not vals.get('baseer_summary_id'):
                    raise AccessError(_('Dedicated summary sessions can only be created by approving an external summary.'))
                summary = self.env['baseer.pos.summary'].browse(vals['baseer_summary_id'])
                summary._lock()
                summary._require_draft()
                if summary.config_id != config or not config.baseer_summary_only:
                    raise ValidationError(_('The native session must use the summary dedicated POS configuration.'))
        return super().create(vals_list)

    def write(self, vals):
        guarded = {'baseer_summary_id', 'config_id', 'state', 'start_at', 'stop_at', 'move_id',
                   'cash_register_balance_start', 'cash_register_balance_end_real', 'order_ids'}
        target_config = self.env['pos.config'].browse(vals.get('config_id'))
        if not internal(self) and ('baseer_summary_id' in vals or target_config.baseer_summary_only
                                   or self.filtered('baseer_summary_id') and guarded.intersection(vals)):
            raise AccessError(_('External summary session accounting and links are controlled by summary approval.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('baseer_summary_id'):
            raise AccessError(_('A linked external summary session cannot be deleted.'))
        return super().unlink()

    def _set_opening_control_data(self, cashbox_value, notes):
        if self.baseer_summary_id and not internal(self):
            raise AccessError(_('Open the dedicated session through external summary approval.'))
        result = super()._set_opening_control_data(cashbox_value, notes)
        if self.baseer_summary_id:
            # No accounting has been created at opening. This native documented
            # extension point runs the normal opening/lock checks first.
            self.start_at = self.baseer_summary_id._business_timestamp()
        return result

    def _create_account_move(self, balancing_account=False, amount_to_balance=0, bank_payment_method_diffs=None):
        summary = self.baseer_summary_id
        if summary:
            if not internal(self) or balancing_account or amount_to_balance or any((bank_payment_method_diffs or {}).values()):
                raise AccessError(_('External summary closing does not allow manual balancing differences.'))
            if summary.session_id != self or self.order_ids != summary.order_id:
                raise ValidationError(_('A dedicated external summary session must contain exactly its original summary order.'))
            summary._check_journal_dates()
        data = super()._create_account_move(balancing_account, amount_to_balance, bank_payment_method_diffs)
        if summary:
            if self.move_id.state != 'draft':
                raise ValidationError(_('The native session entry must still be draft before applying its business date.'))
            self.move_id.date = summary.business_date
            # Native _validate_session catches an unbalanced move then rolls
            # back the entire cursor. Raise here first so our surrounding
            # savepoint preserves caller state and atomic failure semantics.
            if self.move_id._get_unbalanced_moves({'records': self.move_id}):
                raise ValidationError(_('The native summary session entry is unbalanced; approval was rolled back.'))
        return data

    def _create_combine_account_payment(self, payment_method, amounts, diff_amount):
        if self.baseer_summary_id:
            # Native account.payment.create omits date in this combined path;
            # supply its normal ORM default BEFORE it creates/posts its move.
            session = self.with_context(default_date=self.baseer_summary_id.business_date)
            return super(PosSession, session)._create_combine_account_payment(payment_method, amounts, diff_amount)
        return super()._create_combine_account_payment(payment_method, amounts, diff_amount)

    def _get_combine_statement_line_vals(self, journal, amount, payment_method):
        vals = super()._get_combine_statement_line_vals(journal, amount, payment_method)
        if self.baseer_summary_id:
            vals['date'] = self.baseer_summary_id.business_date
        return vals


class PosOrder(models.Model):
    _inherit = 'pos.order'

    baseer_summary_id = fields.Many2one('baseer.pos.summary', readonly=True, copy=False, ondelete='restrict', index=True)
    source = fields.Selection(selection_add=[('baseer_summary', 'External Summary')], ondelete={'baseer_summary': 'set default'})
    _baseer_summary_unique = models.Constraint('unique(baseer_summary_id)', 'A summary may own only one POS order.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            session = self.env['pos.session'].browse(vals.get('session_id') or self.env.context.get('default_session_id'))
            if session.baseer_summary_id or session.config_id.baseer_summary_only or 'baseer_summary_id' in vals or 'default_baseer_summary_id' in self.env.context or vals.get('source', self.env.context.get('default_source')) == 'baseer_summary':
                if not internal(self) or not session.baseer_summary_id or vals.get('baseer_summary_id') != session.baseer_summary_id.id or vals.get('source') != 'baseer_summary':
                    raise AccessError(_('External summary orders can only be created by the authorized summary approval.'))
        return super().create(vals_list)

    def write(self, vals):
        guarded = {'source', 'baseer_summary_id', 'session_id', 'company_id', 'date_order', 'lines', 'payment_ids',
                   'amount_total', 'amount_tax', 'amount_paid', 'amount_return', 'pricelist_id', 'fiscal_position_id',
                   'partner_id', 'state', 'account_move', 'to_invoice'}
        target_session = self.env['pos.session'].browse(vals.get('session_id'))
        if not internal(self) and ('baseer_summary_id' in vals or vals.get('source') == 'baseer_summary'
                                   or target_session.baseer_summary_id or target_session.config_id.baseer_summary_only
                                   or self.filtered('baseer_summary_id') and guarded.intersection(vals)):
            raise AccessError(_('Original external summary order amounts and links cannot be edited directly.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('baseer_summary_id'):
            raise AccessError(_('Original external summary orders cannot be deleted.'))
        return super().unlink()

    def _mark_summary_corrected(self):
        """Mark retained POS evidence after native accounting was fully reversed.

        Native POS write forbids all done->cancel transitions. This narrowly
        scoped extension skips that check for state alone; it neither deletes
        payments nor regenerates accounting, which the correction already owns.
        """
        self.ensure_one()
        summary = self.baseer_summary_id
        if (not internal(self) or not summary or summary.state != 'cancelled'
                or self != summary.order_id or not summary.reversal_move_ids
                or summary.reversal_move_ids.reversed_entry_id != summary._native_moves()
                or any(move.state != 'posted' for move in summary.reversal_move_ids)):
            raise AccessError(_('Only a fully reversed summary correction may cancel its retained POS order.'))
        return super(NativePosOrder, self).write({'state': 'cancel'})


class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            orders = self.env['pos.order'].browse([v.get('order_id') or self.env.context.get('default_order_id') for v in vals_list if v.get('order_id') or self.env.context.get('default_order_id')])
            if orders.filtered('baseer_summary_id'):
                raise AccessError(_('External summary order lines are controlled by summary approval.'))
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self) and (self.order_id.filtered('baseer_summary_id') or vals.get('order_id') and self.env['pos.order'].browse(vals['order_id']).baseer_summary_id):
            raise AccessError(_('External summary order lines cannot be edited directly.'))
        return super().write(vals)

    def unlink(self):
        # The native @ondelete check may be skipped by the uninstall context.
        # Also reject before native edit-tracking writes any source metadata.
        if self.order_id.filtered('baseer_summary_id'):
            raise AccessError(_('Original external summary order lines cannot be deleted.'))
        return super().unlink()


class PosPayment(models.Model):
    _inherit = 'pos.payment'

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            orders = self.env['pos.order'].browse([v.get('pos_order_id') or self.env.context.get('default_pos_order_id') for v in vals_list if v.get('pos_order_id') or self.env.context.get('default_pos_order_id')])
            if orders.filtered('baseer_summary_id'):
                raise AccessError(_('External summary POS payments are controlled by summary approval.'))
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self) and (self.pos_order_id.filtered('baseer_summary_id') or vals.get('pos_order_id') and self.env['pos.order'].browse(vals['pos_order_id']).baseer_summary_id):
            raise AccessError(_('External summary POS payments cannot be edited directly.'))
        return super().write(vals)

    def unlink(self):
        if self.pos_order_id.filtered('baseer_summary_id'):
            raise AccessError(_('External summary POS payments cannot be deleted.'))
        return super().unlink()
