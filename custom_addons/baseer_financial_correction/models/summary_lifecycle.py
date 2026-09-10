"""Atomic source-summary edit/cancel using its existing native reversal engine."""
import json
import uuid

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.baseer_pos_summary.models.common import positive_amount, money

from .dispatcher import can_correct, require_access

_FACTORY = object()
_KEY = '_baseer_summary_lifecycle_factory'


def snapshot(summary):
    moves = summary._native_moves()
    matches = moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids
    tax = summary.config_id.baseer_summary_tax_id
    return json.dumps({
        'id': summary.id, 'company': summary.company_id.id, 'state': summary.state,
        'write_date': str(summary.write_date), 'date': str(summary.business_date),
        'config': summary.config_id.id, 'period': summary.period_scope,
        'config_write_date': str(summary.config_id.write_date), 'schedule': summary.day_schedule,
        'tax': (tax.id, str(tax.write_date), [(r.id, r.account_id.id, r.factor_percent, str(r.write_date))
                                           for r in tax.invoice_repartition_line_ids]),
        'tax_financial': (tax.amount, tax.amount_type, tax.price_include, tax.include_base_amount,
                          tax.tax_exigibility, tax.active, tax.has_negative_factor),
        'customer_count': summary.customer_count, 'zero_sales': summary.zero_sales,
        'replacement': summary.replacement_id.id, 'reversals': summary.reversal_move_ids.ids,
        'allocations': [{'id': a.id, 'method': a.payment_method_id.id, 'amount': str(money(a.amount)),
                         'payment': a.pos_payment_id.id,
                         'method_write_date': str(a.payment_method_id.write_date),
                         'journal': a.payment_method_id.journal_id.id,
                         'journal_write_date': str(a.payment_method_id.journal_id.write_date),
                         'cash_account': a.payment_method_id.journal_id.default_account_id.id,
                         'outstanding_account': a.payment_method_id.outstanding_account_id.id}
                        for a in summary.allocation_ids.sorted('id')],
        'matches': [(m.id, m.debit_move_id.id, m.credit_move_id.id, str(money(m.amount))) for m in matches.sorted('id')],
        'moves': [{'id': m.id, 'state': m.state, 'write_date': str(m.write_date),
                   'reversals': m.reversal_move_ids.ids} for m in moves.sorted('id')],
    }, sort_keys=True)


class SummaryLifecycle(models.Model):
    _inherit = 'baseer.pos.summary'

    baseer_can_correct_operation = fields.Boolean(compute='_compute_baseer_can_correct_operation')

    @api.depends_context('uid')
    def _compute_baseer_can_correct_operation(self):
        for summary in self:
            summary.baseer_can_correct_operation = can_correct(self.env)

    def _check_correction_access(self):
        require_access(self)

    def _prepare_correction_replacement_values(self, values):
        values = super()._prepare_correction_replacement_values(values)
        if self.env.context.get(_KEY) is _FACTORY:
            inputs = self.env.context.get('_baseer_summary_replacement')
            if inputs:
                count, zero_sales, amounts = inputs
                values.update(customer_count=count, zero_sales=zero_sales,
                    allocation_ids=[Command.create({'payment_method_id': line.payment_method_id.id,
                        'amount': float(amounts[line.payment_method_id.id])}) for line in self.allocation_ids])
        return values

    def action_open_correction(self):
        self.ensure_one()
        self._check_correction_access()
        self._lock()
        if self.state != 'approved':
            raise UserError(_('Only an approved summary can be corrected.'))
        if len(self.allocation_ids) > 25:
            raise UserError(_('A summary correction supports at most 25 payment methods.'))
        self.flush_recordset()
        self.allocation_ids.flush_recordset()
        self._native_moves().flush_recordset()
        context = {'allowed_company_ids': self.company_id.ids, 'lang': self.env.lang,
                   'tz': self.env.context.get('tz'), _KEY: _FACTORY}
        wizard = self.env['baseer.pos.summary.correction'].with_context(context).create({
            'summary_id': self.id, 'baseline_json': snapshot(self),
            'operation_token': str(uuid.uuid4()), 'customer_count_input': str(self.customer_count),
            'zero_sales': self.zero_sales,
            'line_ids': [Command.create({'original_allocation_id': line.id,
                        'amount_input': str(money(line.amount))}) for line in self.allocation_ids],
        })
        return {'type': 'ir.actions.act_window', 'name': _('Edit / cancel operation'),
                'res_model': wizard._name, 'res_id': wizard.id,
                'view_mode': 'form', 'target': 'new',
                'context': {'allowed_company_ids': self.company_id.ids, 'edit': True, 'form_view_initial_mode': 'edit'}}

    def action_baseer_cancel_operation(self):
        action = self.action_open_correction()
        self.env['baseer.pos.summary.correction'].browse(action['res_id']).write({'operation': 'cancel'})
        return action


class SummaryCorrectionUsers(models.Model):
    _inherit = 'res.users'

    def write(self, vals):
        result = super().write(vals)
        if 'baseer_allow_financial_correction' in vals:
            self.env['baseer.pos.summary'].invalidate_model(['baseer_can_correct_operation'])
        return result


class SummaryLifecycleWizard(models.TransientModel):
    _inherit = 'baseer.pos.summary.correction'

    company_id = fields.Many2one(related='summary_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    operation = fields.Selection([('edit', 'Edit'), ('cancel', 'Cancel operation')], default='edit', required=True)
    customer_count_input = fields.Char(string='Customer count')
    zero_sales = fields.Boolean(string='Worked with no sales')
    line_ids = fields.One2many('baseer.pos.summary.correction.line', 'wizard_id')
    acknowledge_bookkeeping = fields.Boolean(string='I confirm this corrects the bookkeeping, not a real-money refund')
    baseline_json = fields.Text(readonly=True)
    operation_token = fields.Char(readonly=True)
    completed = fields.Boolean(readonly=True)
    audit_id = fields.Many2one('baseer.financial.correction.audit', readonly=True)
    move_id = fields.Many2one(related='summary_id.move_id', readonly=True)
    payment_id = fields.Many2one('account.payment', readonly=True)
    batch_line_id = fields.Many2one('baseer.purchase.batch.line', readonly=True)
    original_total = fields.Monetary(related='summary_id.amount_gross', currency_field='currency_id')

    _SERVER_FIELDS = {'summary_id', 'company_id', 'currency_id', 'baseline_json', 'operation_token',
                      'completed', 'audit_id', 'move_id', 'payment_id', 'batch_line_id', 'create_uid'}

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_KEY) is not _FACTORY:
            raise AccessError(_('Open summary corrections from the original summary.'))
        return super().create(vals_list)

    def _require_owner(self):
        self.ensure_one()
        self.check_access('write')
        if self.create_uid != self.env.user:
            raise AccessError(_('Only the correction creator can use this request.'))
        require_access(self.summary_id)

    def _lock_requests(self):
        self.check_access('write')
        self.flush_recordset()
        if self:
            self.env.cr.execute('SELECT id FROM baseer_pos_summary_correction WHERE id IN %s ORDER BY id FOR UPDATE',
                                [tuple(sorted(self.ids))])
            # A line-only edit must also advance the parent MVCC version, so a
            # waiting confirmation retries with a fresh repeatable-read snapshot.
            self.env.cr.execute('UPDATE baseer_pos_summary_correction SET id=id WHERE id IN %s', [tuple(self.ids)])
            self.invalidate_recordset()

    def write(self, vals):
        self._lock_requests()
        if self._SERVER_FIELDS.intersection(vals):
            raise AccessError(_('Summary correction source and history are managed by the system.'))
        for wizard in self:
            wizard._require_owner()
            if wizard.completed:
                raise UserError(_('This correction has already been completed.'))
            for command in vals.get('line_ids', []):
                if (not isinstance(command, (tuple, list)) or len(command) != 3
                        or command[0] != Command.UPDATE or command[1] not in wizard.line_ids.ids
                        or not isinstance(command[2], dict) or set(command[2]) - {'amount_input'}):
                    raise AccessError(_('Only the amounts of original payment methods can be edited.'))
        return super().write(vals)

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('Summary correction requests must be retained until automatic cleanup.'))

    def _validated_replacement_values(self):
        self.ensure_one()
        raw = self.customer_count_input or ''
        if len(raw) > 8 or not raw.isascii() or not raw.isdecimal() or not 0 <= int(raw) <= 10000000:
            raise ValidationError(_('Customer count must be a whole number between 0 and 10000000.'))
        if (len(self.line_ids) != len(self.summary_id.allocation_ids)
                or set(self.line_ids.original_allocation_id.ids) != set(self.summary_id.allocation_ids.ids)):
            raise UserError(_('The correction must contain exactly the original payment methods.'))
        amounts = {line.original_allocation_id.payment_method_id.id:
                   positive_amount(line.amount_input, self.env, allow_zero=True) for line in self.line_ids}
        if self.zero_sales and (int(raw) or any(amounts.values())):
            raise ValidationError(_('A no-sales operation must have zero sales and zero customers.'))
        return int(raw), amounts

    def action_correct(self):
        self.ensure_one()
        self._require_owner()
        with self.env.cr.savepoint():
            self._lock_requests()
            self._require_owner()
            summary = self.summary_id
            summary._lock()
            if self.completed:
                return summary._source_action(summary.replacement_id or summary, _('Sales summary'))
            if snapshot(summary) != self.baseline_json:
                raise UserError(_('The source changed after this correction was opened. Close it and open a fresh correction.'))
            if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason.strip()) > 2000:
                raise ValidationError(_('Enter a correction reason of at most 2000 characters.'))
            if not self.acknowledge_bookkeeping:
                raise ValidationError(_('Confirm the bookkeeping effect before continuing.'))
            inputs = self._validated_replacement_values() if self.operation == 'edit' else None
            before = snapshot(summary)
            context = {'allowed_company_ids': summary.company_id.ids, 'lang': self.env.lang,
                       'tz': self.env.context.get('tz'), _KEY: _FACTORY}
            if inputs:
                context['_baseer_summary_replacement'] = (inputs[0], self.zero_sales, inputs[1])
            summary.with_context(context)._correct_summary(self.reason, create_replacement=self.operation == 'edit')
            replacement = summary.replacement_id
            if inputs:
                replacement.action_approve()
                if replacement.state != 'approved':
                    raise ValidationError(_('The corrected summary was not approved.'))
            elif replacement:
                raise ValidationError(_('A cancelled summary must not create a replacement.'))
            from .correction import CORRECTION_CAPABILITY
            after = json.dumps({'original': json.loads(snapshot(summary)),
                'replacement': json.loads(snapshot(replacement)) if replacement else False}, sort_keys=True)
            audit = self.env['baseer.financial.correction.audit']._record_correction(
                self, before, after, CORRECTION_CAPABILITY)
            super(SummaryLifecycleWizard, self).write({'completed': True, 'audit_id': audit.id})
            return summary._source_action(replacement or summary, _('Sales summary'))


class SummaryLifecycleLine(models.TransientModel):
    _name = 'baseer.pos.summary.correction.line'
    _description = 'Summary correction payment method'

    wizard_id = fields.Many2one('baseer.pos.summary.correction', required=True, ondelete='cascade', readonly=True)
    company_id = fields.Many2one(related='wizard_id.company_id', store=True)
    original_allocation_id = fields.Many2one('baseer.pos.summary.allocation', required=True, readonly=True)
    payment_method_id = fields.Many2one(related='original_allocation_id.payment_method_id', readonly=True)
    original_amount = fields.Monetary(related='original_allocation_id.amount', currency_field='currency_id')
    currency_id = fields.Many2one(related='wizard_id.currency_id')
    operation = fields.Selection(related='wizard_id.operation')
    completed = fields.Boolean(related='wizard_id.completed')
    amount_input = fields.Char(string='Correct amount', required=True)
    account_name = fields.Char(compute='_compute_account_name', string='Account')

    @api.depends('payment_method_id')
    def _compute_account_name(self):
        for line in self:
            method = line.payment_method_id
            account = method.journal_id.default_account_id if method.type == 'cash' else method.outstanding_account_id
            line.account_name = account.display_name

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_KEY) is not _FACTORY:
            raise AccessError(_('Summary correction rows are prepared by the system.'))
        return super().create(vals_list)

    def write(self, vals):
        self.wizard_id._lock_requests()
        if set(vals) - {'amount_input'}:
            raise AccessError(_('The original payment method cannot be changed.'))
        for wizard in self.wizard_id:
            wizard._require_owner()
            if wizard.completed:
                raise UserError(_('This correction has already been completed.'))
        if 'amount_input' in vals:
            positive_amount(vals['amount_input'], self.env, allow_zero=True)
        return super().write(vals)

    def unlink(self):
        if self.env.su:
            return super().unlink()
        raise AccessError(_('The original payment method cannot be removed.'))
