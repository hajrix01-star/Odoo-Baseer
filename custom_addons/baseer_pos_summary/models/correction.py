from collections import defaultdict
from odoo import _, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError

from .common import clean_context, manager, money, ZERO


class PosSummaryCorrection(models.TransientModel):
    _name = 'baseer.pos.summary.correction'
    _description = 'Reviewed sales summary correction'

    summary_id = fields.Many2one('baseer.pos.summary', required=True, readonly=True)
    reason = fields.Text()

    def action_correct(self):
        self.ensure_one()
        self.check_access('read')
        if self.create_uid != self.env.user:
            raise AccessError(_('Only the correction creator can use this request.'))
        return self.summary_id._correct_summary(self.reason)


class PosSummary(models.Model):
    _inherit = 'baseer.pos.summary'

    correction_reason = fields.Text(readonly=True, copy=False)
    corrected_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    corrected_at = fields.Datetime(readonly=True, copy=False)
    replacement_id = fields.Many2one('baseer.pos.summary', readonly=True, copy=False, ondelete='restrict', check_company=True)
    replaces_id = fields.Many2one('baseer.pos.summary', readonly=True, copy=False, ondelete='restrict', check_company=True)
    reversal_move_ids = fields.Many2many('account.move', 'baseer_pos_summary_reversal_rel', 'summary_id', 'move_id', readonly=True, copy=False)

    def _check_correction_access(self):
        manager(self)

    def _prepare_correction_replacement_values(self, values):
        return values

    def action_open_correction(self):
        self.ensure_one()
        self._check_correction_access()
        self._lock()
        if self.state == 'cancelled' and self.replacement_id:
            return self._source_action(self.replacement_id, _('Replacement sales summary'))
        if self.state != 'approved':
            raise UserError(_('Only an approved summary can be corrected.'))
        request = self.env['baseer.pos.summary.correction'].create({'summary_id': self.id})
        return {'type': 'ir.actions.act_window', 'res_model': request._name, 'res_id': request.id,
                'view_mode': 'form', 'target': 'new'}

    def _correct_summary(self, reason, create_replacement=True):
        self.ensure_one()
        self._check_correction_access()
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 2000:
            raise ValidationError(_('Enter a correction reason of at most 2000 characters.'))
        with self.env.cr.savepoint():
            self._lock()
            self._serialize_company()
            if self.state == 'cancelled' and not self.replacement_id and not create_replacement:
                return self._source_action(self, _('Cancelled sales summary'))
            if self.state == 'cancelled' and self.replacement_id:
                return self._source_action(self.replacement_id, _('Replacement sales summary'))
            if self.state != 'approved':
                raise UserError(_('Only an approved summary can be corrected.'))
            self._check_journal_dates()
            scoped = clean_context(self, self.company_id, authorized_internal=True)
            moves = scoped._native_moves()
            if not self.zero_sales and (not moves or not self.move_id):
                raise UserError(_('The original summary accounting is incomplete; review it before correction.'))
            if any(move.state != 'posted' or move.reversal_move_ids for move in moves):
                raise UserError(_('An original entry is no longer posted or already has a reversal. Review the previous receipt or accounting correction before correcting sales.'))
            matches = moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids
            if (matches.debit_move_id | matches.credit_move_id).move_id - moves:
                raise UserError(_('External settlements are linked to this summary. Review and undo those settlements using their original accounting workflow before correcting sales.'))
            # The same business date is intentional. A locked period must never
            # be silently corrected in another period or erase external matches.
            reversals = moves._reverse_moves(default_values_list=[{'date': self.business_date,
                'ref': _('Correction of %s: %s', self.name, reason.strip()), 'auto_post': 'no'} for move in moves], cancel=True)
            exact = len(reversals) == len(moves) and set(reversals.reversed_entry_id.ids) == set(moves.ids)
            for reversal in reversals:
                original = reversal.reversed_entry_id
                balances = defaultdict(lambda: [ZERO, ZERO])
                for line in (original | reversal).line_ids:
                    key = (line.account_id.id, line.partner_id.id, line.currency_id.id)
                    balances[key][0] += money(line.balance)
                    balances[key][1] += money(line.amount_currency)
                exact = exact and not any(any(amounts) for amounts in balances.values())
                exact = exact and reversal.company_id == self.company_id and reversal.journal_id == original.journal_id
            if (not exact or any(move.state != 'posted' or move.date != self.business_date for move in reversals)):
                raise ValidationError(_('The native correction entries did not reverse the original summary exactly.'))
            vals = {'company_id': self.company_id.id, 'config_id': self.config_id.id,
                'business_date': self.business_date, 'period_scope': self.period_scope, 'day_schedule': self.day_schedule,
                'customer_count': self.customer_count, 'zero_sales': self.zero_sales, 'notes': self.notes,
                'external_reference': self.external_reference, 'attachment_ids': [Command.set(self.attachment_ids.ids)],
                'allocation_ids': [Command.create({'payment_method_id': line.payment_method_id.id, 'amount': line.amount}) for line in self.allocation_ids]}
            # These narrowly scoped super writes bypass only summary RPC input
            # protection. Normal create still validates the replacement values.
            scoped._write_correction_metadata({'state': 'cancelled', 'correction_reason': reason.strip(),
                'corrected_by_id': self.env.uid, 'corrected_at': fields.Datetime.now(),
                'reversal_move_ids': [Command.set(reversals.ids)]})
            if scoped.order_id:
                scoped.order_id._mark_summary_corrected()
            if not create_replacement:
                return self._source_action(self, _('Cancelled sales summary'))
            replacement = self.env['baseer.pos.summary'].create(self._prepare_correction_replacement_values(vals))
            clean_context(replacement, self.company_id, authorized_internal=True)._write_correction_metadata({'replaces_id': self.id})
            scoped._write_correction_metadata({'replacement_id': replacement.id})
            return self._source_action(replacement, _('Review replacement sales summary'))
