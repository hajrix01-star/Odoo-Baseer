"""Keep source-owned purchase operations together at native mutation boundaries."""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

_TOKEN = object()
_KEY = '_baseer_lifecycle_token'
_MOVES = '_baseer_lifecycle_moves'
_PAYMENTS = '_baseer_lifecycle_payments'


def lifecycle_scope(record, move, payment):
    """Python-only scope, populated from the already validated/locked operation."""
    return record.with_context(**{_KEY: _TOKEN, _MOVES: tuple((move | payment.move_id).ids),
                                 _PAYMENTS: tuple(payment.ids)})


def permitted(record):
    key = _MOVES if record._name == 'account.move' else _PAYMENTS
    return record.env.context.get(_KEY) is _TOKEN and set(record.ids).issubset(
        record.env.context.get(key, ()))


class LifecycleMove(models.Model):
    _inherit = 'account.move'

    baseer_unified_reset = fields.Boolean(compute='_compute_baseer_unified_reset')

    @api.depends('state', 'matched_payment_ids.state')
    def _compute_baseer_unified_reset(self):
        owned = self._baseer_purchase_owned()
        for move in self:
            move.baseer_unified_reset = move in owned or bool(
                move.move_type in ('in_invoice', 'out_invoice')
                and move._baseer_linked_posted_payments())

    def _baseer_linked_posted_payments(self):
        lines = self.line_ids.filtered(lambda line: line.account_id.account_type in ('asset_receivable', 'liability_payable'))
        partials = lines.matched_debit_ids | lines.matched_credit_ids
        others = (partials.debit_move_id.move_id | partials.credit_move_id.move_id) - self
        return (self.matched_payment_ids | others.origin_payment_id).filtered(lambda payment: payment.move_id.state == 'posted')

    def _baseer_purchase_owned(self):
        if not self:
            return self
        # Fixed existence check only; no confidential source values are returned.
        rows = self.env['baseer.purchase.batch.line'].sudo().search([
            '|', ('move_id', 'in', self.ids), ('payment_id.move_id', 'in', self.ids)])
        ids = set(rows.move_id.ids) | set(rows.payment_id.move_id.ids)
        return self.filtered(lambda move: move.id in ids)

    def _baseer_guard_source_lifecycle(self, include_paid_invoice=False):
        if permitted(self):
            return
        owned = self._baseer_purchase_owned()
        if owned:
            raise UserError(_('Use Correct operation or Cancel operation from the purchase row. Its invoice and payment must stay together.'))
        if include_paid_invoice:
            # The journal entry is another native route to the same payment.
            self.origin_payment_id._baseer_guard_payment_lifecycle(related_invoice=True)
            invoices = self.filtered(lambda move: move.move_type in ('in_invoice', 'out_invoice')
                                     and move.state in ('posted', 'cancel'))
            if invoices and invoices.sudo()._baseer_linked_posted_payments():
                raise UserError(_('This invoice has a posted payment. Use the unified operation action to handle both records.'))

    def button_draft(self):
        self._baseer_guard_source_lifecycle(include_paid_invoice=True)
        return super().button_draft()

    def button_cancel(self):
        self._baseer_guard_source_lifecycle(include_paid_invoice=True)
        return super().button_cancel()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        from odoo.addons.baseer_pos_summary.models.common import internal
        if self.filtered('baseer_pos_summary_id') and not internal(self):
            raise UserError(_('Use the sales summary operation to reverse its sales and receipts together.'))
        self._baseer_guard_source_lifecycle()
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)

    @api.model_create_multi
    def create(self, vals_list):
        originals = self.browse([vals.get('reversed_entry_id') or self.env.context.get('default_reversed_entry_id')
            for vals in vals_list if vals.get('reversed_entry_id') or self.env.context.get('default_reversed_entry_id')])
        originals._baseer_guard_source_lifecycle()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('reversed_entry_id'):
            self.browse(vals['reversed_entry_id'])._baseer_guard_source_lifecycle()
        if 'origin_payment_id' in vals:
            self._baseer_guard_source_lifecycle(include_paid_invoice=True)
            if vals['origin_payment_id']:
                self.env['account.payment'].browse(vals['origin_payment_id'])._baseer_guard_payment_lifecycle(related_invoice=True)
        if 'matched_payment_ids' in vals:
            # Native registration records the persistent link after reconciling.
            # Only allow additive links already proved by the accounting graph.
            commands = vals['matched_payment_ids']
            additive = bool(commands) and all(
                isinstance(command, (list, tuple)) and len(command) >= 2
                and command[0] == 4 for command in commands)
            if additive:
                for move in self:
                    lines = move.line_ids.filtered(lambda line: line.account_id.account_type in ('asset_receivable', 'liability_payable'))
                    partials = lines.matched_debit_ids | lines.matched_credit_ids
                    others = (partials.debit_move_id.move_id | partials.credit_move_id.move_id) - move
                    if not {command[1] for command in commands}.issubset(set(others.origin_payment_id.ids)):
                        additive = False
                        break
            if not additive:
                self._baseer_guard_source_lifecycle(include_paid_invoice=True)
        financial = {'state', 'line_ids', 'invoice_line_ids', 'partner_id', 'currency_id',
                     'company_id', 'journal_id', 'date', 'invoice_date', 'move_type',
                     'origin_payment_id', 'reversed_entry_id', 'ref', 'payment_reference'}
        if financial.intersection(vals):
            self._baseer_guard_source_lifecycle(include_paid_invoice=vals.get('state') in ('draft', 'cancel'))
        return super().write(vals)

    def unlink(self):
        self._baseer_guard_source_lifecycle(include_paid_invoice=True)
        return super().unlink()


class LifecycleMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        moves = self.env['account.move'].browse([vals.get('move_id') or self.env.context.get('default_move_id')
            for vals in vals_list if vals.get('move_id') or self.env.context.get('default_move_id')])
        moves._baseer_guard_source_lifecycle()
        return super().create(vals_list)

    def write(self, vals):
        if {'move_id', 'account_id', 'partner_id', 'debit', 'credit', 'balance', 'amount_currency',
                'currency_id', 'company_id', 'date', 'quantity', 'price_unit', 'discount',
                'tax_ids', 'tax_line_id', 'tax_repartition_line_id', 'tax_tag_ids', 'analytic_distribution',
                'name', 'display_type', 'product_id'}.intersection(vals):
            (self.move_id | self.env['account.move'].browse(vals.get('move_id')))._baseer_guard_source_lifecycle()
        return super().write(vals)

    def unlink(self):
        self.move_id._baseer_guard_source_lifecycle()
        return super().unlink()


class LifecyclePayment(models.Model):
    _inherit = 'account.payment'

    @api.model_create_multi
    def create(self, vals_list):
        moves = self.env['account.move'].browse([
            vals.get('move_id') or self.env.context.get('default_move_id')
            for vals in vals_list if vals.get('move_id') or self.env.context.get('default_move_id')])
        moves._baseer_guard_source_lifecycle()
        return super().create(vals_list)

    def _baseer_guard_payment_lifecycle(self, related_invoice=False):
        if permitted(self):
            return
        self.move_id._baseer_guard_source_lifecycle()
        if related_invoice:
            for payment in self.filtered(lambda item: item.move_id.state == 'posted'):
                counterpart = payment._seek_for_lines()[1]
                partials = counterpart.matched_debit_ids | counterpart.matched_credit_ids
                linked = (partials.debit_move_id.move_id | partials.credit_move_id.move_id) - payment.move_id
                if payment.invoice_ids or linked.filtered(lambda move: move.is_invoice()):
                    raise UserError(_('This payment is linked to an invoice. Use Cancel operation or Correct operation to keep the source consistent.'))

    def action_draft(self):
        self._baseer_guard_payment_lifecycle(related_invoice=True)
        return super().action_draft()

    def action_cancel(self):
        self._baseer_guard_payment_lifecycle(related_invoice=True)
        return super().action_cancel()

    def write(self, vals):
        if vals.get('move_id'):
            self.env['account.move'].browse(vals['move_id'])._baseer_guard_source_lifecycle()
        if 'invoice_ids' in vals:
            self._baseer_guard_payment_lifecycle(related_invoice=True)
        if {'amount', 'partner_id', 'journal_id', 'payment_method_line_id', 'company_id',
                'currency_id', 'payment_type', 'partner_type', 'move_id', 'invoice_ids'}.intersection(vals):
            self._baseer_guard_payment_lifecycle()
        if vals.get('state') in ('draft', 'canceled'):
            self._baseer_guard_payment_lifecycle(related_invoice=True)
        return super().write(vals)

    def unlink(self):
        self._baseer_guard_payment_lifecycle(related_invoice=True)
        return super().unlink()
