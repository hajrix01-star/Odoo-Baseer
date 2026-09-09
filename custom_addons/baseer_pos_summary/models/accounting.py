"""Owned journal evidence is immutable; native reconciliation stays available."""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .common import internal, clean_context, manager


class AccountMove(models.Model):
    _inherit = 'account.move'

    baseer_pos_summary_id = fields.Many2one('baseer.pos.summary', readonly=True, copy=False,
        check_company=True, index=True, ondelete='restrict')
    _BASEER_FINANCIAL = {'baseer_pos_summary_id', 'line_ids', 'date', 'journal_id', 'company_id',
        'state', 'move_type', 'partner_id', 'currency_id', 'reversed_entry_id', 'origin_payment_id',
        'statement_line_id', 'pos_session_ids', 'amount_total', 'amount_untaxed', 'amount_tax', 'ref',
        'name', 'auto_post', 'invoice_date', 'invoice_line_ids'}

    def _baseer_guard_financial(self):
        if not internal(self) and self.sudo().filtered('baseer_pos_summary_id'):
            raise UserError(_('Summary journal evidence cannot be edited, reset or deleted. Use Correct summary from its original sales summary.'))

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            if 'default_baseer_pos_summary_id' in self.env.context or any('baseer_pos_summary_id' in vals for vals in vals_list):
                raise AccessError(_('Summary accounting ownership is controlled by the server.'))
            originals = self.browse([vals.get('reversed_entry_id') or self.env.context.get('default_reversed_entry_id')
                for vals in vals_list if vals.get('reversed_entry_id') or self.env.context.get('default_reversed_entry_id')])
            originals._baseer_guard_financial()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('reversed_entry_id') and not internal(self):
            self.browse(vals['reversed_entry_id'])._baseer_guard_financial()
        if self._BASEER_FINANCIAL.intersection(vals):
            if not internal(self) and 'baseer_pos_summary_id' in vals:
                raise AccessError(_('Summary accounting ownership is controlled by the server.'))
            self._baseer_guard_financial()
        return super().write(vals)

    def button_draft(self):
        self._baseer_guard_financial()
        return super().button_draft()

    def button_cancel(self):
        self._baseer_guard_financial()
        return super().button_cancel()

    def unlink(self):
        self._baseer_guard_financial()
        return super().unlink()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        owned = self.filtered('baseer_pos_summary_id')
        if not owned:
            return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)
        if not internal(self):
            manager(self)
            owned.baseer_pos_summary_id._lock()
            for move in owned:
                summary = move.baseer_pos_summary_id
                if (move == summary.move_id or move.reversed_entry_id or move.reversal_move_ids
                        or move.state != 'posted' or not cancel):
                    raise UserError(_('Use Correct summary to reverse sales. Only an unreversed original receipt can be reversed separately.'))
        # Ownership travels to the immutable native counter-entry. No caller can
        # inject it through create/default context, and the token never leaves.
        defaults = [dict(vals) for vals in (default_values_list or [{} for _ in self])]
        for move, vals in zip(self, defaults):
            if move.baseer_pos_summary_id:
                vals['baseer_pos_summary_id'] = move.baseer_pos_summary_id.id
        if len(self.company_id) != 1:
            raise UserError(_('Reverse summary receipts one company at a time.'))
        scoped = clean_context(self, self.company_id, authorized_internal=True)
        result = super(AccountMove, scoped)._reverse_moves(default_values_list=defaults, cancel=cancel)
        return self.browse(result.ids)


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    _BASEER_FINANCIAL = {'move_id', 'account_id', 'company_id', 'currency_id', 'partner_id', 'date',
        'debit', 'credit', 'balance', 'amount_currency', 'product_id', 'quantity', 'price_unit',
        'discount', 'tax_ids', 'tax_line_id', 'tax_repartition_line_id', 'tax_tag_ids', 'display_type',
        'name', 'analytic_distribution', 'price_subtotal', 'price_total'}

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            moves = self.env['account.move'].browse([vals.get('move_id') or self.env.context.get('default_move_id')
                for vals in vals_list if vals.get('move_id') or self.env.context.get('default_move_id')])
            moves._baseer_guard_financial()
        return super().create(vals_list)

    def write(self, vals):
        if self._BASEER_FINANCIAL.intersection(vals) and not internal(self):
            (self.move_id | self.env['account.move'].browse(vals.get('move_id')))._baseer_guard_financial()
        return super().write(vals)

    def unlink(self):
        self.move_id._baseer_guard_financial()
        return super().unlink()


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            for vals in vals_list:
                session = self.env['pos.session'].browse(vals.get('pos_session_id') or self.env.context.get('default_pos_session_id'))
                if session.sudo().baseer_summary_id:
                    raise AccessError(_('Summary receipt links are controlled by the server.'))
                self.env['account.move'].browse(vals.get('move_id') or self.env.context.get('default_move_id'))._baseer_guard_financial()
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self) and self.env['pos.session'].browse(vals.get('pos_session_id')).sudo().baseer_summary_id:
            raise AccessError(_('Summary receipt links are controlled by the server.'))
        if {'pos_session_id', 'move_id', 'company_id', 'amount', 'currency_id', 'date',
                'journal_id', 'partner_id', 'payment_type', 'partner_type',
                'destination_account_id', 'outstanding_account_id'}.intersection(vals):
            (self.move_id | self.env['account.move'].browse(vals.get('move_id')))._baseer_guard_financial()
        return super().write(vals)

    def unlink(self):
        self.move_id._baseer_guard_financial()
        return super().unlink()


class AccountBankStatementLine(models.Model):
    _inherit = 'account.bank.statement.line'

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            for vals in vals_list:
                session = self.env['pos.session'].browse(vals.get('pos_session_id') or self.env.context.get('default_pos_session_id'))
                if session.sudo().baseer_summary_id:
                    raise AccessError(_('Summary receipt links are controlled by the server.'))
                self.env['account.move'].browse(vals.get('move_id') or self.env.context.get('default_move_id'))._baseer_guard_financial()
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self) and self.env['pos.session'].browse(vals.get('pos_session_id')).sudo().baseer_summary_id:
            raise AccessError(_('Summary receipt links are controlled by the server.'))
        if {'pos_session_id', 'move_id', 'company_id', 'amount', 'currency_id', 'date',
                'journal_id', 'partner_id', 'payment_ref', 'foreign_currency_id',
                'amount_currency'}.intersection(vals):
            (self.move_id | self.env['account.move'].browse(vals.get('move_id')))._baseer_guard_financial()
        return super().write(vals)

    def unlink(self):
        self.move_id._baseer_guard_financial()
        return super().unlink()
