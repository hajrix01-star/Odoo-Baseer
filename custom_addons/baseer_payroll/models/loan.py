"""Employee advances. Native receivable residuals are the balance authority."""
from decimal import Decimal
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError

from .common import INTERNAL, decimal, money, validate_money, require_manager, lock_employee


def _internal(record):
    return record.env.context.get('baseer_payroll_internal') is INTERNAL


class BaseerLoan(models.Model):
    _name = 'baseer.hr.loan'
    _description = 'Employee Advance'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default=lambda self: _('Employee advance'), tracking=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    employee_id = fields.Many2one('hr.employee', required=True, check_company=True, index=True, tracking=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    amount = fields.Monetary(required=True, tracking=True)
    date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    first_due_date = fields.Date(required=True, default=fields.Date.context_today)
    installment_count = fields.Integer(default=1, required=True)
    journal_id = fields.Many2one('account.journal', required=True, check_company=True,
                                 domain="[('type', 'in', ['bank', 'cash']), ('company_id', '=', company_id)]")
    state = fields.Selection([('draft', 'Draft'), ('running', 'Outstanding'), ('closed', 'Repaid'), ('cancel', 'Cancelled')],
                             default='draft', required=True, readonly=True, index=True, tracking=True, copy=False)
    move_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, ondelete='restrict')
    reversal_move_id = fields.Many2one('account.move', readonly=True, copy=False, ondelete='restrict')
    line_ids = fields.One2many('baseer.hr.loan.line', 'loan_id', readonly=True, copy=False)
    allocation_ids = fields.One2many('baseer.hr.loan.allocation', 'loan_id', readonly=True, copy=False)
    defer_ids = fields.One2many('baseer.hr.loan.defer', 'loan_id', readonly=True, copy=False)
    paid_amount = fields.Monetary(compute='_compute_balances', store=True)
    balance = fields.Monetary(compute='_compute_balances', store=True)

    @api.depends('amount', 'move_id.line_ids.amount_residual', 'move_id.line_ids.debit',
                 'move_id.line_ids.account_id.reconcile', 'move_id.state', 'state', 'reversal_move_id')
    def _compute_balances(self):
        for loan in self:
            residual = sum((decimal(line.amount_residual) for line in loan._principal_lines()), Decimal('0'))
            # An unissued draft is a request, not an employee receivable.
            loan.balance = float(money(residual if loan.move_id else 0))
            loan.paid_amount = float(money(decimal(loan.amount) - decimal(loan.balance))) if loan.move_id and not loan.reversal_move_id else 0

    def _principal_lines(self):
        self.ensure_one()
        return self.move_id.line_ids.filtered(lambda line: line.debit > 0 and line.account_id.reconcile)

    @api.constrains('amount', 'installment_count', 'employee_id', 'company_id', 'date', 'first_due_date', 'journal_id')
    def _check_setup(self):
        for loan in self:
            validate_money(loan.amount)
            if decimal(loan.amount) <= 0:
                raise ValidationError(_('The advance amount must be greater than zero.'))
            if loan.installment_count < 1 or loan.installment_count > 120:
                raise ValidationError(_('Choose between 1 and 120 installments.'))
            if money(decimal(loan.amount) / loan.installment_count) <= 0:
                raise ValidationError(_('The amount is too small for this number of installments.'))
            if loan.employee_id.company_id != loan.company_id or loan.journal_id.company_id != loan.company_id:
                raise ValidationError(_('The employee and payment journal must belong to the advance company.'))
            if loan.first_due_date < loan.date:
                raise ValidationError(_('The first installment cannot precede the advance date.'))

    @api.model_create_multi
    def create(self, vals_list):
        require_manager(self)
        protected = ('move_id', 'reversal_move_id', 'line_ids', 'allocation_ids', 'defer_ids', 'state', 'paid_amount', 'balance', 'currency_id')
        defaults = self.default_get(list(protected) + ['company_id', 'employee_id', 'amount'])
        for vals in vals_list:
            effective = dict(defaults, **vals)
            validate_money(effective.get('amount', 0))
            if not _internal(self):
                if effective.get('state', 'draft') != 'draft' or any(effective.get(k) for k in protected if k != 'state'):
                    raise AccessError(_('Advance financial history is managed by the payroll workflow.'))
            company = self.env['res.company'].browse(effective.get('company_id') or self.env.company.id)
            if company != self.env.company:
                raise AccessError(_('You cannot create an advance for this company.'))
            if effective.get('employee_id'):
                lock_employee(self.env, effective['employee_id'], company.id)
        return super().create(vals_list)

    def _lock(self):
        for loan in self.sorted(lambda l: (l.company_id.id, l.employee_id.id, l.id)):
            lock_employee(self.env, loan.employee_id.id, loan.company_id.id)
            self.env.cr.execute('UPDATE baseer_hr_loan SET write_date=write_date WHERE id=%s', [loan.id])
        self.invalidate_recordset()

    def write(self, vals):
        if _internal(self):
            return super().write(vals)
        require_manager(self)
        self._lock()
        if set(vals) & {'move_id', 'reversal_move_id', 'line_ids', 'allocation_ids', 'defer_ids', 'state', 'paid_amount', 'balance', 'currency_id'}:
            raise AccessError(_('Advance financial history is managed by the payroll workflow.'))
        business = {'name', 'company_id', 'employee_id', 'amount', 'date', 'first_due_date', 'installment_count', 'journal_id'}
        if set(vals) & business and any(loan.state != 'draft' for loan in self):
            raise UserError(_('An issued or cancelled advance cannot be edited.'))
        if 'amount' in vals:
            validate_money(vals['amount'])
        if 'employee_id' in vals or 'company_id' in vals:
            for loan in self:
                if vals.get('company_id', loan.company_id.id) != self.env.company.id:
                    raise AccessError(_('Switch to the company that owns this record.'))
                lock_employee(self.env, vals.get('employee_id', loan.employee_id.id), vals.get('company_id', loan.company_id.id))
        return super().write(vals)

    def unlink(self):
        require_manager(self)
        self._lock()
        if any(loan.move_id or loan.state not in ('draft', 'cancel') for loan in self):
            raise UserError(_('Issued advances and their history cannot be deleted.'))
        return super().unlink()

    def _accounts(self, journal):
        self.ensure_one()
        company = self.company_id
        account = self._principal_lines().account_id if self.move_id else company.baseer_loan_account_id
        if self.currency_id.decimal_places != 2:
            raise UserError(_('Employee advances currently require a currency with two decimal places.'))
        if not account or not account.reconcile or company not in account.company_ids or account.account_type not in ('asset_receivable', 'asset_current', 'asset_non_current'):
            raise UserError(_('Configure a reconcilable employee advance account for this company.'))
        if journal.company_id != company or journal.type not in ('bank', 'cash'):
            raise UserError(_('Choose a bank or cash journal belonging to this company.'))
        cash = journal.default_account_id
        if not cash or cash == account or company not in cash.company_ids or cash.account_type != 'asset_cash':
            raise UserError(_('Configure a separate default treasury account on the payment journal.'))
        if journal.currency_id and journal.currency_id != company.currency_id:
            raise UserError(_('Use a payment journal in the company currency.'))
        if not self.employee_id.work_contact_id:
            raise UserError(_('Set a work contact on the employee before issuing an advance.'))
        return account, cash

    def _make_move(self, amount, date, journal, repayment=False):
        self.ensure_one()
        account, cash = self._accounts(journal)
        date = fields.Date.to_date(date)
        if self.company_id._get_violated_lock_dates(date, False, journal):
            raise UserError(_('This advance transaction falls in a locked accounting period. Choose an open transaction date.'))
        partner = self.employee_id.work_contact_id
        amount = float(money(amount))
        debit, credit = (cash, account) if repayment else (account, cash)
        label = _('%s — %s', self.name, _('Advance repayment') if repayment else _('Advance disbursement'))
        move = self.env['account.move'].with_company(self.company_id).with_context(baseer_payroll_internal=INTERNAL).create({
            'move_type': 'entry', 'date': date, 'journal_id': journal.id, 'company_id': self.company_id.id,
            'ref': label, 'baseer_loan_id': self.id,
            'line_ids': [Command.create({'name': label, 'account_id': debit.id, 'partner_id': partner.id, 'debit': amount}),
                         Command.create({'name': label, 'account_id': credit.id, 'partner_id': partner.id, 'credit': amount})],
        })
        move.action_post()
        if move.date != date:
            raise ValidationError(_('The accounting date changed during posting. The advance transaction was not recorded; choose an open date.'))
        return move

    def action_disburse(self):
        require_manager(self)
        self._lock()
        for loan in self:
            if loan.move_id and loan.state in ('running', 'closed'):
                continue
            if loan.state != 'draft':
                raise UserError(_('Only a draft advance can be issued.'))
            move = loan._make_move(loan.amount, loan.date, loan.journal_id)
            amount = money(loan.amount)
            installment = money(amount / loan.installment_count)
            # Allocate cents to the final installment; never lose or create principal.
            last = amount - installment * (loan.installment_count - 1)
            if last <= 0:
                raise ValidationError(_('Use fewer installments for this advance amount.'))
            loan.with_context(baseer_payroll_internal=INTERNAL).write({'move_id': move.id, 'state': 'running', 'line_ids': [
                Command.create({'sequence': i + 1, 'amount': float(last if i == loan.installment_count - 1 else installment),
                                'due_date': loan.first_due_date + relativedelta(months=i),
                                'original_due_date': loan.first_due_date + relativedelta(months=i)})
                for i in range(loan.installment_count)
            ]})
            loan.message_post(body=_('Advance issued. The journal entry records the cash or bank disbursement; no external transfer is sent.'))
        return True

    def action_cancel(self):
        require_manager(self)
        self._lock()
        if any(loan.state != 'draft' or loan.move_id for loan in self):
            raise UserError(_('Only draft advances can be cancelled. Issued advances require a controlled accounting correction.'))
        self.with_context(baseer_payroll_internal=INTERNAL).write({'state': 'cancel'})
        return True

    def action_repay(self):
        self.ensure_one()
        require_manager(self)
        if self.state != 'running':
            raise UserError(_('Only an outstanding advance can be repaid.'))
        return {'type': 'ir.actions.act_window', 'name': _('Repay advance'), 'res_model': 'baseer.hr.loan.repay',
                'view_mode': 'form', 'target': 'new', 'context': {'default_loan_id': self.id, 'default_amount': self.balance}}

    def action_view_moves(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Advance accounting history'), 'res_model': 'account.move',
                'view_mode': 'list,form', 'domain': [('id', 'in', (self.move_id | self.allocation_ids.move_id).ids)]}

    def _due_lines(self, date_to):
        return self.filtered(lambda l: l.state == 'running').line_ids.filtered(
            lambda line: line.due_date <= fields.Date.to_date(date_to) and money(line.balance) > 0
        ).sorted(lambda line: (line.due_date, line.loan_id.date, line.loan_id.id, line.sequence))

    def _due_amount(self, date_to):
        return money(sum((decimal(line.balance) for line in self._due_lines(date_to)), Decimal('0')))

    def _defer_due(self, date_to, reason):
        require_manager(self)
        self._lock()
        if not reason or not reason.strip():
            raise ValidationError(_('Enter a reason for deferring an installment.'))
        for line in self._due_lines(date_to):
            # Arrears deferred during this run must move beyond this run's cutoff.
            anchor = max(line.due_date, fields.Date.to_date(date_to))
            next_date = anchor + relativedelta(months=1)
            self.env['baseer.hr.loan.defer'].with_context(baseer_payroll_internal=INTERNAL).create({
                'loan_id': line.loan_id.id, 'line_id': line.id, 'old_date': line.due_date,
                'new_date': next_date, 'reason': reason.strip(), 'user_id': self.env.user.id,
            })
            line.with_context(baseer_payroll_internal=INTERNAL).write({'due_date': next_date})
            line.loan_id.message_post(body=_('Installment deferred to %s. Reason: %s', next_date, reason))

    def _allocate(self, amount, move, date, kind, payslip=False, cutoff=False):
        """Allocate and reconcile explicitly so one loan never consumes another loan's credit."""
        remaining = money(amount)
        candidates = self._due_lines(cutoff) if cutoff else self.line_ids.filtered(
            lambda line: money(line.balance) > 0).sorted(lambda line: (line.due_date, line.loan_id.id, line.sequence))
        for line in candidates:
            if remaining <= 0:
                break
            loan = line.loan_id
            portion = min(remaining, money(line.balance))
            account = loan._principal_lines().account_id
            credits = move.line_ids.filtered(lambda ml: ml.account_id == account and ml.partner_id == loan.employee_id.work_contact_id and ml.credit > 0)
            needed = portion
            for debit in loan._principal_lines():
                for credit in credits:
                    take = min(needed, money(debit.amount_residual), money(-decimal(credit.amount_residual)))
                    if take <= 0:
                        continue
                    self.env['account.partial.reconcile'].with_context(baseer_payroll_internal=INTERNAL).create({
                        'debit_move_id': debit.id, 'credit_move_id': credit.id, 'amount': float(take),
                        'debit_amount_currency': float(take), 'credit_amount_currency': float(take),
                    })
                    needed -= take
                    if needed <= 0:
                        break
                if needed <= 0:
                    break
            if needed:
                raise ValidationError(_('The advance recovery does not match its receivable journal entry.'))
            self.env['baseer.hr.loan.allocation'].with_context(baseer_payroll_internal=INTERNAL).create({
                'loan_id': loan.id, 'line_id': line.id, 'amount': float(portion), 'date': date,
                'move_id': move.id, 'payslip_id': payslip.id if payslip else False, 'kind': kind,
            })
            remaining -= portion
        if remaining:
            raise ValidationError(_('The repayment exceeds the available installments.'))
        for loan in self:
            loan.invalidate_recordset(['balance', 'paid_amount'])
            if money(loan.balance) == 0 and loan.state == 'running':
                loan.with_context(baseer_payroll_internal=INTERNAL).write({'state': 'closed'})
            recovered = sum((decimal(a.amount) for a in loan.allocation_ids.filtered(lambda a: not a.reversal_move_id)), Decimal('0'))
            if money(recovered) != money(loan.paid_amount):
                raise ValidationError(_('The advance schedule and accounting balance do not agree.'))

    def _apply_payroll(self, slip, move):
        require_manager(self)
        self._lock()
        if not slip.baseer_managed or move.state != 'posted' or move.company_id != slip.company_id:
            raise ValidationError(_('A posted payroll entry is required to recover advances.'))
        if any(l.company_id != slip.company_id or l.employee_id != slip.employee_id for l in self):
            raise ValidationError(_('Advance recovery must match the payslip employee and company.'))
        amount = money(slip.baseer_loan_amount)
        if amount <= 0:
            return
        if self.env['baseer.hr.loan.allocation'].search_count([('payslip_id', '=', slip.id)]):
            raise UserError(_('Advance deductions have already been recorded for this payslip.'))
        if amount > self._due_amount(slip.date_to):
            raise ValidationError(_('Advance installments changed. Refresh the draft payroll before approving it.'))
        if self.allocation_ids and slip.date_to < max(self.allocation_ids.mapped('date')):
            raise ValidationError(_('This payroll period precedes an already recorded advance recovery. Process recoveries in date order.'))
        self._allocate(amount, move, slip.date_to, 'payroll', payslip=slip, cutoff=slip.date_to)


class LoanInstallment(models.Model):
    _name = 'baseer.hr.loan.line'
    _description = 'Advance Installment'
    _order = 'due_date, sequence, id'
    _rec_name = 'sequence'

    loan_id = fields.Many2one('baseer.hr.loan', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='loan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='loan_id.currency_id')
    employee_id = fields.Many2one(related='loan_id.employee_id', store=True, index=True)
    sequence = fields.Integer(required=True)
    amount = fields.Monetary(required=True)
    due_date = fields.Date(required=True, index=True)
    original_due_date = fields.Date(required=True)
    allocation_ids = fields.One2many('baseer.hr.loan.allocation', 'line_id')
    paid_amount = fields.Monetary(compute='_compute_balance', store=True)
    balance = fields.Monetary(compute='_compute_balance', store=True)
    state = fields.Selection([('pending', 'Pending'), ('partial', 'Partially recovered'), ('paid', 'Recovered'), ('cancel', 'Cancelled')], compute='_compute_balance', store=True)

    @api.depends('amount', 'allocation_ids.amount', 'allocation_ids.reversal_move_id', 'loan_id.reversal_move_id')
    def _compute_balance(self):
        for line in self:
            paid = money(sum((decimal(a.amount) for a in line.allocation_ids.filtered(lambda a: not a.reversal_move_id)), Decimal('0')))
            if line.loan_id.reversal_move_id:
                line.paid_amount, line.balance, line.state = 0, 0, 'cancel'
                continue
            balance = money(decimal(line.amount) - paid)
            line.paid_amount, line.balance = float(paid), float(balance)
            line.state = 'paid' if balance == 0 else ('partial' if paid else 'pending')

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_('Installments are created by the advance workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        if not _internal(self):
            raise AccessError(_('Use the payroll defer action to change an installment.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda l: l.loan_id.move_id):
            raise UserError(_('Issued advance installments cannot be deleted.'))
        if not _internal(self):
            raise AccessError(_('Installments are managed by the advance workflow.'))
        return super().unlink()


class LoanAllocation(models.Model):
    _name = 'baseer.hr.loan.allocation'
    _description = 'Advance Recovery History'
    _order = 'date desc, id desc'

    loan_id = fields.Many2one('baseer.hr.loan', required=True, ondelete='restrict', index=True)
    line_id = fields.Many2one('baseer.hr.loan.line', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='loan_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='loan_id.currency_id')
    amount = fields.Monetary(required=True)
    date = fields.Date(required=True, index=True)
    move_id = fields.Many2one('account.move', required=True, ondelete='restrict', index=True)
    payslip_id = fields.Many2one('hr.payslip', ondelete='restrict', index=True)
    kind = fields.Selection([('payroll', 'Payroll deduction'), ('repayment', 'Direct repayment')], required=True)
    reversal_move_id = fields.Many2one('account.move', readonly=True, copy=False, ondelete='restrict')

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_('Advance recoveries are created only by payroll or repayment.'))
        records = super().create(vals_list)
        for row in records:
            if row.amount <= 0 or row.line_id.loan_id != row.loan_id or row.move_id.company_id != row.company_id:
                raise ValidationError(_('Invalid advance recovery allocation.'))
        return records

    def write(self, vals):
        if _internal(self) and set(vals) == {'reversal_move_id'} and not self.filtered('reversal_move_id'):
            return super().write(vals)
        raise AccessError(_('Advance recovery history is immutable.'))

    def unlink(self):
        raise AccessError(_('Advance recovery history cannot be deleted.'))


class LoanDefer(models.Model):
    _name = 'baseer.hr.loan.defer'
    _description = 'Advance Deferral History'
    _order = 'date desc, id desc'

    loan_id = fields.Many2one('baseer.hr.loan', required=True, ondelete='restrict', index=True)
    line_id = fields.Many2one('baseer.hr.loan.line', required=True, ondelete='restrict')
    company_id = fields.Many2one(related='loan_id.company_id', store=True, index=True)
    old_date = fields.Date(required=True)
    new_date = fields.Date(required=True)
    reason = fields.Char(required=True)
    date = fields.Datetime(default=fields.Datetime.now, required=True)
    user_id = fields.Many2one('res.users', default=lambda self: self.env.user, required=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            raise AccessError(_('Deferral history is created by the payroll workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_('Deferral history is immutable.'))

    def unlink(self):
        raise AccessError(_('Deferral history cannot be deleted.'))


class LoanRepay(models.TransientModel):
    _name = 'baseer.hr.loan.repay'
    _description = 'Repay Employee Advance'

    loan_id = fields.Many2one('baseer.hr.loan', required=True, readonly=True)
    company_id = fields.Many2one(related='loan_id.company_id')
    currency_id = fields.Many2one(related='loan_id.currency_id')
    amount = fields.Monetary(required=True)
    date = fields.Date(default=fields.Date.context_today, required=True)
    journal_id = fields.Many2one('account.journal', required=True, domain="[('type', 'in', ['bank', 'cash']), ('company_id', '=', company_id)]")
    move_id = fields.Many2one('account.move', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        require_manager(self)
        defaults = self.default_get(['amount', 'move_id'])
        for vals in vals_list:
            effective = dict(defaults, **vals)
            validate_money(effective.get('amount', 0))
            if effective.get('move_id'):
                raise AccessError(_('Repayment results cannot be supplied manually.'))
        return super().create(vals_list)

    def write(self, vals):
        if not _internal(self):
            require_manager(self)
            self.loan_id._lock()
            self.invalidate_recordset()
            if 'loan_id' in vals or 'move_id' in vals or self.filtered('move_id'):
                raise AccessError(_('A completed repayment cannot be changed.'))
            if 'amount' in vals:
                validate_money(vals['amount'])
        return super().write(vals)

    def action_confirm(self):
        self.ensure_one()
        require_manager(self.loan_id)
        loan = self.loan_id
        loan._lock()
        self.invalidate_recordset()
        if self.move_id:
            return {'type': 'ir.actions.act_window_close'}
        amount = money(self.amount)
        if loan.state != 'running' or amount <= 0 or amount > money(loan.balance):
            raise ValidationError(_('Enter a positive repayment no greater than the outstanding advance.'))
        if self.date < loan.date or (loan.allocation_ids and self.date < max(loan.allocation_ids.mapped('date'))):
            raise ValidationError(_('The repayment date cannot precede the advance or an already recorded recovery.'))
        move = loan._make_move(amount, self.date, self.journal_id, repayment=True)
        loan._allocate(amount, move, self.date, 'repayment')
        self.with_context(baseer_payroll_internal=INTERNAL).write({'move_id': move.id})
        loan.message_post(body=_('Direct repayment recorded: %s. Journal entry: %s', amount, move.display_name))
        return {'type': 'ir.actions.act_window_close'}


class LoanAccountMove(models.Model):
    _inherit = 'account.move'

    baseer_loan_id = fields.Many2one('baseer.hr.loan', readonly=True, copy=False, ondelete='restrict', index=True)

    @api.model_create_multi
    def create(self, vals_list):
        default_loan = self.default_get(['baseer_loan_id']).get('baseer_loan_id')
        if not _internal(self) and any(vals.get('baseer_loan_id', default_loan) for vals in vals_list):
            raise AccessError(_('Advance journal entries must be created through the advance workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        if not _internal(self):
            if 'baseer_loan_id' in vals:
                raise AccessError(_('Advance source links cannot be changed.'))
            if self.filtered(lambda m: m.baseer_loan_id and m.state == 'posted') and set(vals) & {
                'state', 'date', 'journal_id', 'company_id', 'currency_id', 'line_ids', 'partner_id', 'move_type',
            }:
                raise UserError(_('Posted advance entries are protected. Use a controlled advance correction.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('baseer_loan_id'):
            raise UserError(_('Advance journal entries cannot be deleted.'))
        return super().unlink()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        if self.filtered('baseer_loan_id'):
            raise UserError(_('Advance reversal is not available until its schedule and recoveries can be corrected together.'))
        return super()._reverse_moves(default_values_list=default_values_list, cancel=cancel)


class LoanAccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            default_move = self.default_get(['move_id']).get('move_id')
            move_ids = [v.get('move_id', default_move) for v in vals_list]
            moves = self.env['account.move'].browse([move_id for move_id in move_ids if move_id])
            if moves.filtered(lambda m: m.baseer_loan_id and m.state == 'posted'):
                raise UserError(_('Lines cannot be added to posted advance entries.'))
        return super().create(vals_list)

    def write(self, vals):
        moves = self.move_id | self.env['account.move'].browse(vals.get('move_id') or [])
        if not _internal(self) and moves.filtered(lambda move: move.baseer_loan_id and move.state == 'posted') and set(vals) & {
            'debit', 'credit', 'balance', 'amount_currency', 'currency_id', 'account_id', 'partner_id', 'move_id',
            'company_id', 'date', 'tax_ids', 'tax_line_id', 'reconciled', 'matched_debit_ids', 'matched_credit_ids',
            'amount_residual', 'amount_residual_currency',
        }:
            raise UserError(_('Posted advance journal lines are protected.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered(lambda l: l.move_id.baseer_loan_id):
            raise UserError(_('Advance journal lines cannot be deleted.'))
        return super().unlink()


class LoanPartialReconcile(models.Model):
    _inherit = 'account.partial.reconcile'

    @api.model_create_multi
    def create(self, vals_list):
        if not _internal(self):
            keys = ('debit_move_id', 'credit_move_id')
            defaults = self.default_get(list(keys))
            ids = [v.get(key, defaults.get(key)) for v in vals_list for key in keys]
            ids = [line_id for line_id in ids if line_id]
            if self.env['account.move.line'].browse(ids).filtered(lambda l: l.move_id.baseer_loan_id and l.account_id.reconcile and l.account_id.account_type != 'asset_cash'):
                raise UserError(_('Recover employee advances using payroll or the advance repayment action so the installment history remains consistent.'))
        return super().create(vals_list)

    def write(self, vals):
        financial = {'amount', 'debit_amount_currency', 'credit_amount_currency', 'debit_move_id', 'credit_move_id', 'company_id'}
        destinations = self.env['account.move.line'].browse([
            vals[key] for key in ('debit_move_id', 'credit_move_id') if vals.get(key)
        ])
        affected = self.debit_move_id | self.credit_move_id | destinations
        if financial.intersection(vals) and affected.filtered(lambda l: l.move_id.baseer_loan_id and l.account_id.account_type != 'asset_cash'):
            raise UserError(_('Advance recovery reconciliations cannot be changed manually.'))
        return super().write(vals)

    def unlink(self):
        if not _internal(self) and (self.debit_move_id | self.credit_move_id).filtered(lambda l: l.move_id.baseer_loan_id and l.account_id.account_type != 'asset_cash'):
            raise UserError(_('Advance recovery reconciliations cannot be removed independently of their repayment history.'))
        return super().unlink()
