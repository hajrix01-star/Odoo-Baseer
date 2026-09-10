"""Restricted advance entry; native payroll loans own all accounting and balances."""
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.addons.baseer_payroll.models.common import ADVANCE_DISBURSE, validate_money


INPUT_FIELDS = frozenset({'name', 'company_id', 'employee_id', 'amount', 'date',
                          'first_due_date', 'installment_count', 'journal_id'})


class AdvanceEntry(models.Model):
    _name = 'baseer.advance.entry'
    _description = 'Employee Advance Entry'
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default=lambda self: _('Employee advance'))
    company_id = fields.Many2one('res.company', required=True, index=True,
                                default=lambda self: self.env.company)
    employee_id = fields.Many2one('hr.employee.public', string='Employee', required=True,
                                 index=True, check_company=True, ondelete='restrict')
    currency_id = fields.Many2one(related='company_id.currency_id')
    amount = fields.Monetary(required=True)
    date = fields.Date(required=True, default=fields.Date.context_today)
    first_due_date = fields.Date(required=True, default=fields.Date.context_today)
    installment_count = fields.Integer(required=True, default=1)
    journal_id = fields.Many2one('account.journal', required=True, check_company=True,
                                domain="[('type', 'in', ['bank', 'cash']), ('company_id', '=', company_id), ('active', '=', True)]")
    loan_id = fields.Many2one('baseer.hr.loan', readonly=True, copy=False,
                             ondelete='restrict', groups='base.group_system')
    state = fields.Selection([('draft', 'Draft'), ('running', 'Outstanding'),
                              ('closed', 'Repaid'), ('cancel', 'Cancelled')],
                             compute='_compute_loan_summary')
    balance = fields.Monetary(compute='_compute_loan_summary')
    loan_reference = fields.Char(string='Advance Reference', compute='_compute_loan_summary')

    def _require_operator(self):
        if not any(self.env.user.has_group('baseer_access_roles.group_' + role)
                   for role in ('owner', 'accountant', 'cashier')):
            raise AccessError(_('An assigned owner, accountant or cashier role is required.'))

    def _clean_context(self):
        # Never forward client defaults, skip flags, alternate users or accounting contexts.
        company = self.env.company
        if company not in self.env.user.company_ids:
            raise AccessError(_('The company is outside your authorized companies.'))
        return {'allowed_company_ids': [company.id], 'lang': self.env.user.lang,
                'tz': self.env.user.tz}

    def _validate_input_keys(self, values):
        if set(values) - INPUT_FIELDS or any(
                key.startswith('default_') and key[8:] not in INPUT_FIELDS for key in self.env.context):
            raise AccessError(_('Advance references and financial results are managed by the system.'))
        if 'amount' in values:
            # Validate the caller's precision before Monetary storage rounds it.
            validate_money(values['amount'])

    def _validate_setup(self):
        """Validate the public employee projection and journal without payroll elevation."""
        for entry in self:
            if entry.company_id != self.env.company or entry.company_id not in self.env.user.company_ids:
                raise AccessError(_('Switch to the company that owns this record.'))
            entry.employee_id.check_access('read')
            entry.journal_id.check_access('read')
            # hr.employee.public is the authoritative SQL projection of the same employee ID.
            if not entry.employee_id.active or entry.employee_id.company_id != entry.company_id:
                raise AccessError(_('Choose an active employee belonging to this company.'))
            if (not entry.journal_id.active or entry.journal_id.company_id != entry.company_id
                    or entry.journal_id.type not in ('cash', 'bank')):
                raise AccessError(_('Choose an active bank or cash journal belonging to this company.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_operator()
        for vals in vals_list:
            self._validate_input_keys(vals)
        with self.env.cr.savepoint():
            clean_self = self.with_context(self._clean_context())
            entries = super(AdvanceEntry, clean_self).create(vals_list)
            entries._validate_setup()
        return entries

    def _lock_entries(self):
        self.check_access('write')
        for entry in self.sorted('id'):
            self.env.cr.execute('SELECT id FROM baseer_advance_entry WHERE id=%s FOR UPDATE', [entry.id])
        self.invalidate_recordset()
        self.check_access('write')

    def write(self, vals):
        self._require_operator()
        self._validate_input_keys(vals)
        with self.env.cr.savepoint():
            self._lock_entries()
            self._validate_setup()
            if any(self.sudo().mapped('loan_id')):
                raise UserError(_('An issued advance entry cannot be edited or deleted.'))
            clean_self = self.with_context(self._clean_context())
            result = super(AdvanceEntry, clean_self).write(vals)
            clean_self._validate_setup()
        return result

    def unlink(self):
        self._require_operator()
        self.check_access('unlink')
        with self.env.cr.savepoint():
            self._lock_entries()
            self._validate_setup()
            if any(self.sudo().mapped('loan_id')):
                raise UserError(_('An issued advance entry cannot be edited or deleted.'))
            result = super().unlink()
        return result

    @api.depends('loan_id', 'loan_id.state', 'loan_id.balance', 'loan_id.name')
    def _compute_loan_summary(self):
        for entry in self:
            entry.state, entry.balance, entry.loan_reference = 'draft', 0, False
            if not entry.id:
                continue
            entry.check_access('read')
            if entry.company_id not in self.env.user.company_ids:
                raise AccessError(_('The company is outside your authorized companies.'))
            loan = entry.sudo().loan_id
            if loan:
                entry.state = loan.state
                entry.balance = loan.balance
                entry.loan_reference = loan.name

    def action_disburse(self):
        self.ensure_one()
        self._require_operator()
        self.check_access('read')
        self.check_access('write')
        with self.env.cr.savepoint():
            self._lock_entries()
            self._validate_setup()
            context = self._clean_context()
            if self.sudo().loan_id:
                # The link is written only after successful native posting in this same transaction.
                self.invalidate_recordset(['state', 'balance', 'loan_reference'])
                return True
            values = {key: self[key] for key in (
                'name', 'amount', 'date', 'first_due_date', 'installment_count')}
            values.update(company_id=self.company_id.id, employee_id=self.employee_id.id,
                          journal_id=self.journal_id.id)
            context['baseer_advance_disburse'] = ADVANCE_DISBURSE
            loan = self.env['baseer.hr.loan'].with_context(context).sudo().create(values)
            loan.action_disburse()
            # Bypass only this wrapper's immutable-input guard, never the native accounting flow.
            super(AdvanceEntry, self.sudo().with_context(self._clean_context())).write({'loan_id': loan.id})
            self.invalidate_recordset(['state', 'balance', 'loan_reference'])
        return True
