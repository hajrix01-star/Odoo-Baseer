"""Independent inherited-policy EOS estimate and one explicit native award bill."""
from decimal import Decimal
from datetime import timedelta
from dateutil.relativedelta import relativedelta
from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.misc import clean_context
from .common import INTERNAL, decimal, money, require_manager, lock_employee


REASONS = [('termination', 'Employer termination'), ('resignation', 'Resignation'),
           ('article80', 'Article 80'), ('article81', 'Article 81'), ('force', 'Force majeure'),
           ('maternity', 'Maternity'), ('review', 'Other reviewed reason')]
SPECIAL = {'article80', 'article81', 'force', 'maternity', 'review'}
LEGACY_POLICY = 'SA-EOS-V1 / Baseer 2dp'
POLICY = 'SA-EOS-V2 / Gregorian anniversaries / Baseer 2dp'


def eos_formula(start, end, wage, reason, policy=LEGACY_POLICY, inclusive=False):
    """Explicit versioned calendar policy; historical V1 remains reproducible."""
    start, end = fields.Date.to_date(start), fields.Date.to_date(end)
    stop = end + timedelta(days=1) if inclusive and policy == POLICY else end
    days = (stop - start).days
    if days <= 0 or decimal(wage) <= 0:
        raise ValidationError(_('Choose a positive service period and a positive fixed wage.'))
    if policy == POLICY:
        whole = stop.year - start.year
        if start + relativedelta(years=whole) > stop:
            whole -= 1
        anniversary = start + relativedelta(years=whole)
        next_anniversary = start + relativedelta(years=whole + 1)
        years = Decimal(whole) + Decimal((stop - anniversary).days) / Decimal((next_anniversary - anniversary).days)
    elif policy == LEGACY_POLICY:
        years = Decimal(days) / Decimal(365)
    else:
        raise ValidationError(_('Choose a supported calculation policy.'))
    full = decimal(wage) * (min(years, Decimal(5)) / 2 + max(years - 5, Decimal(0)))
    if reason not in dict(REASONS):
        raise ValidationError(_('Choose a supported end-of-service reason.'))
    factor = Decimal(1)
    if reason == 'article80':
        factor = Decimal(0)
    elif reason == 'resignation':
        factor = Decimal(0) if years < 2 else Decimal(1) / 3 if years <= 5 else Decimal(2) / 3 if years < 10 else Decimal(1)
    return days, money(full), factor, money(full * factor)


def internal(record):
    return record.env.context.get('baseer_payroll_internal') is INTERNAL


class EosCompany(models.Model):
    _inherit = 'res.company'
    baseer_eos_expense_id = fields.Many2one('account.account', string='End-of-service expense')
    baseer_eos_journal_id = fields.Many2one('account.journal', string='End-of-service purchase journal')


class EosSettings(models.TransientModel):
    _inherit = 'res.config.settings'
    baseer_eos_expense_id = fields.Many2one(related='company_id.baseer_eos_expense_id', readonly=False)
    baseer_eos_journal_id = fields.Many2one(related='company_id.baseer_eos_journal_id', readonly=False)


class EosRequest(models.Model):
    _name = 'baseer.hr.eos'
    _description = 'End-of-Service Award'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'service_end desc, id desc'
    _rec_name = 'employee_id'
    _approved_period_unique = models.UniqueIndex(
        '(employee_id, service_start, service_end) WHERE state = \'approved\'',
        'This employee already has an approved award for this service period.')
    _approved_departure_unique = models.UniqueIndex(
        '(employee_id, service_end) WHERE state = \'approved\'',
        'This employee departure already has an approved award.')

    employee_id = fields.Many2one('hr.employee', required=True, index=True, context={'active_test': False})
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    version_id = fields.Many2one('hr.version', required=True, context={'active_test': False})
    service_start = fields.Date(required=True)
    service_end = fields.Date(required=True, default=fields.Date.context_today, index=True)
    reason = fields.Selection(REASONS, default='termination', required=True)
    evidence_reference = fields.Char(string='Evidence reference', size=240)
    evidence_note = fields.Text(string='Review note')
    reason_verified = fields.Boolean(string='Special reason verified')
    approval_confirmed = fields.Boolean(string='I reviewed the policy, wage, service dates and entitlement')
    state = fields.Selection([('draft', 'Estimate'), ('approved', 'Award issued')], default='draft', required=True, readonly=True, index=True, copy=False, tracking=True)
    policy_version = fields.Char(default=POLICY, readonly=True)
    service_end_inclusive = fields.Boolean(default=True, string='Include the final service day')
    gregorian_confirmed = fields.Boolean(string='Contract or regulations specify the Gregorian calendar')
    source_snapshot = fields.Json(readonly=True, copy=False)
    service_days = fields.Integer(readonly=True, copy=False)
    eos_wage = fields.Monetary(string='Fixed wage excluding overtime', readonly=True, copy=False)
    full_award = fields.Monetary(string='Full award', readonly=True, copy=False)
    entitlement_factor = fields.Float(string='Entitlement factor', digits=(16, 8), readonly=True, copy=False)
    award_amount = fields.Monetary(string='Award amount', readonly=True, copy=False)
    bill_id = fields.Many2one('account.move', readonly=True, copy=False, ondelete='restrict')
    bill_state = fields.Selection(related='bill_id.state', string='Bill status')
    payment_state = fields.Selection(related='bill_id.payment_state', string='Payment status')
    balance = fields.Monetary(related='bill_id.amount_residual', string='Remaining to pay')
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)

    @api.onchange('employee_id')
    def _onchange_employee(self):
        for request in self:
            if request.employee_id:
                request.version_id = request.employee_id.version_id
                request.service_start = request.employee_id._get_first_contract_date() or request.version_id.contract_date_start

    @api.onchange('version_id')
    def _onchange_version(self):
        for request in self:
            if request.version_id:
                request.service_start = request.employee_id._get_first_contract_date() or request.version_id.contract_date_start

    @api.model_create_multi
    def create(self, vals_list):
        require_manager(self)
        protected = ['state', 'source_snapshot', 'service_days', 'eos_wage', 'full_award', 'entitlement_factor',
                     'award_amount', 'bill_id', 'approved_by_id', 'approved_at', 'currency_id', 'bill_state', 'payment_state', 'balance']
        defaults = self.default_get(protected + ['employee_id', 'version_id', 'company_id', 'policy_version'])
        prepared = []
        for raw in vals_list:
            vals = dict(raw)
            effective = dict(defaults, **vals)
            if not internal(self) and (effective.get('state', 'draft') != 'draft'
                    or effective.get('policy_version', POLICY) != POLICY
                    or any(effective.get(key) for key in protected if key != 'state')):
                raise AccessError(_('Award calculations and bill links are generated by the approved workflow.'))
            company = self.env['res.company'].browse(effective.get('company_id') or self.env.company.id)
            employee = self.env['hr.employee'].with_context(active_test=False).browse(effective.get('employee_id')).exists()
            if not employee or len(employee) != 1 or company != self.env.company or employee.company_id != company:
                raise AccessError(_('Choose an employee in the active company.'))
            version = self.env['hr.version'].with_context(active_test=False).browse(effective.get('version_id')) or employee.version_id
            vals.setdefault('version_id', version.id)
            vals.setdefault('service_start', employee._get_first_contract_date() or version.contract_date_start)
            lock_employee(self.env, employee.id, company.id)
            prepared.append(vals)
        return super().create(prepared)

    @api.constrains('employee_id', 'version_id', 'company_id', 'service_start', 'service_end')
    def _check_sources(self):
        for request in self:
            if request.company_id != request.employee_id.company_id or request.version_id.employee_id != request.employee_id or request.version_id.company_id != request.company_id:
                raise ValidationError(_('The salary version and employee must belong to the award company.'))
            if request.service_end < request.service_start:
                raise ValidationError(_('The service end must be after the service start.'))

    def _lock(self):
        require_manager(self)
        for request in self.sorted('id'):
            self.env.cr.execute('SELECT id FROM baseer_hr_eos WHERE id=%s FOR UPDATE', [request.id])
        self.invalidate_recordset()
        for request in self.sorted(lambda r: (r.company_id.id, r.employee_id.id)):
            lock_employee(self.env, request.employee_id.id, request.company_id.id)

    def write(self, vals):
        if not internal(self):
            self._lock()
            if any(request.state != 'draft' for request in self):
                raise UserError(_('An issued award and its source evidence cannot be changed. Use the native bill reversal for a reviewed correction.'))
            allowed = {'employee_id', 'version_id', 'service_start', 'service_end', 'reason', 'evidence_reference', 'evidence_note', 'reason_verified', 'approval_confirmed', 'service_end_inclusive', 'gregorian_confirmed'}
            if set(vals) - allowed:
                raise AccessError(_('Award calculations and bill links are protected.'))
            result = super().write(vals)
            if set(vals) & {'employee_id', 'version_id', 'service_start', 'service_end', 'reason', 'service_end_inclusive'}:
                self.with_context(baseer_payroll_internal=INTERNAL).write({'source_snapshot': False,
                    'service_days': 0, 'eos_wage': 0, 'full_award': 0, 'entitlement_factor': 0, 'award_amount': 0,
                    'approval_confirmed': False, 'reason_verified': False, 'gregorian_confirmed': False})
            return result
        return super().write(vals)

    def unlink(self):
        self._lock()
        if self.filtered(lambda request: request.state == 'approved' or request.bill_id):
            raise UserError(_('Approved awards retain their history even if the native bill is cancelled or reversed.'))
        return super().unlink()

    def _source(self):
        self.ensure_one()
        version = self.version_id
        self._check_sources()
        start_versions = self.env['hr.version'].with_context(active_test=False).search([
            ('employee_id', '=', self.employee_id.id), ('contract_date_start', '=', self.service_start)])
        if not start_versions or not version.contract_date_start or self.service_start > version.contract_date_start:
            raise ValidationError(_('Choose a recorded employee contract start date no later than the final salary contract. Review the service period if the employee was rehired.'))
        if version.date_start > self.service_end or (version.date_end and version.date_end < self.service_end):
            raise ValidationError(_('Select the salary version effective on the service end date.'))
        basic, overtime, allowance = version._baseer_split()
        wage = money(basic + allowance)
        if wage <= 0 or self.currency_id.decimal_places != 2:
            raise ValidationError(_('A positive fixed wage and a currency with two decimal places are required.'))
        return {'employee': self.employee_id.id, 'company': self.company_id.id, 'version': version.id,
                'version_date': str(version.date_version), 'version_write_date': str(version.write_date),
                'contract_start': str(version.contract_date_start), 'contract_end': str(version.contract_date_end),
                'service_start_versions': sorted(start_versions.ids),
                'calendar': version.resource_calendar_id.id, 'hours': str(decimal(version.resource_calendar_id.hours_per_day)),
                'gross': str(money(version.wage)), 'basic': str(basic), 'allowance': str(allowance), 'overtime': str(overtime),
                'days_basis': version.baseer_work_days, 'mode': version.baseer_salary_mode,
                'start': str(self.service_start), 'end': str(self.service_end), 'reason': self.reason,
                'currency': self.currency_id.id, 'wage': str(wage), 'policy': self.policy_version,
                'end_inclusive': self.service_end_inclusive}

    def action_use_gregorian_policy(self):
        self._lock()
        if any(request.state != 'draft' for request in self):
            raise UserError(_('An issued award cannot change calculation policy.'))
        self.with_context(baseer_payroll_internal=INTERNAL).write({'policy_version': POLICY,
            'service_end_inclusive': True, 'gregorian_confirmed': False, 'approval_confirmed': False,
            'source_snapshot': False, 'service_days': 0, 'eos_wage': 0, 'full_award': 0,
            'entitlement_factor': 0, 'award_amount': 0})
        return self.action_calculate()

    def _check_current_policy(self):
        if any(request.policy_version != POLICY for request in self):
            raise ValidationError(_('This draft uses the historical policy. Select Gregorian anniversary policy explicitly and review the recalculated estimate.'))

    def action_calculate(self):
        self._lock()
        for request in self:
            if request.state != 'draft':
                raise UserError(_('An issued award cannot be recalculated.'))
            request._check_current_policy()
            source = request._source()
            days, full, factor, award = eos_formula(request.service_start, request.service_end, source['wage'], request.reason,
                                                   request.policy_version, request.service_end_inclusive)
            request.with_context(baseer_payroll_internal=INTERNAL).write({'source_snapshot': source,
                'service_days': days, 'eos_wage': float(decimal(source['wage'])), 'full_award': float(full),
                'entitlement_factor': float(factor), 'award_amount': float(award), 'approval_confirmed': False})
        return True

    def action_approve(self):
        self.ensure_one()
        self._lock()
        if self.state == 'approved':
            return self.action_view_bill()
        self._check_current_policy()
        if not self.gregorian_confirmed:
            raise ValidationError(_('Confirm that the contract or regulations specify Gregorian dates before issuing this award.'))
        if not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_('Accounting access is required to issue an award bill.'))
        # Native FK/row locks serialize HR edits against the final source revalidation.
        for table, record in [('hr_employee', self.employee_id), ('hr_version', self.version_id),
                              ('resource_calendar', self.version_id.resource_calendar_id), ('res_company', self.company_id)]:
            if record:
                self.env.cr.execute('SELECT id FROM ' + table + ' WHERE id=%s FOR UPDATE', [record.id])
                record.invalidate_recordset()
        if self.service_end > fields.Date.context_today(self):
            raise ValidationError(_('An award cannot be issued before the service end date.'))
        version = self.version_id
        if not version.departure_date or version.departure_date != self.service_end or not version.departure_reason_id:
            raise ValidationError(_('HR must record the employee departure with the same end date and a departure reason before issuing the award.'))
        if not self.source_snapshot or self.source_snapshot != self._source():
            raise ValidationError(_('Salary or service information changed. Recalculate the estimate and review it before approval.'))
        if not (self.evidence_reference or '').strip() or not self.approval_confirmed:
            raise ValidationError(_('Enter the evidence reference and confirm that you reviewed the award calculation.'))
        if self.reason in SPECIAL and not self.reason_verified:
            raise ValidationError(_('This reason requires documented verification before approval.'))
        if money(self.award_amount) <= 0:
            raise ValidationError(_('A zero award remains an estimate; there is no amount to bill.'))
        if self.search_count([('id', '!=', self.id), ('employee_id', '=', self.employee_id.id), ('state', '=', 'approved'),
            ('service_start', '<=', self.service_end), ('service_end', '>=', self.service_start)]):
            raise ValidationError(_('An award has already been issued for this employee and service period.'))
        company, partner = self.company_id, self.employee_id.work_contact_id
        journal, expense = company.baseer_eos_journal_id, company.baseer_eos_expense_id
        payable = partner.with_company(company).property_account_payable_id if partner else False
        if not journal or journal.type != 'purchase' or journal.company_id != company:
            raise ValidationError(_('Configure an end-of-service purchase journal for this company in Payroll Settings.'))
        if not expense or company not in expense.company_ids or expense.account_type not in ('expense', 'expense_direct_cost', 'expense_depreciation'):
            raise ValidationError(_('Configure an end-of-service expense account for this company.'))
        if not partner or (partner.company_id and partner.company_id != company) or not payable or not payable.reconcile or payable.account_type != 'liability_payable' or company not in payable.company_ids:
            raise ValidationError(_('The employee work contact needs a valid reconcilable payable account in this company.'))
        if journal.currency_id and journal.currency_id != company.currency_id:
            raise ValidationError(_('The award journal must use the company currency.'))
        if company._get_violated_lock_dates(self.service_end, False, journal):
            raise ValidationError(_('The award accounting date is in a locked period.'))
        bill = self.env['account.move'].with_context(dict(clean_context(self.env.context), baseer_payroll_internal=INTERNAL)).create({
            'move_type': 'in_invoice', 'company_id': company.id, 'partner_id': partner.id,
            'currency_id': company.currency_id.id, 'journal_id': journal.id, 'invoice_date': self.service_end,
            'date': self.service_end, 'invoice_date_due': self.service_end, 'invoice_payment_term_id': False,
            'fiscal_position_id': False, 'baseer_eos_id': self.id, 'ref': _('End-of-service award / %s', self.employee_id.name),
            'invoice_line_ids': [Command.create({'name': _('End-of-service award'), 'product_id': False,
                'account_id': expense.id, 'quantity': 1, 'price_unit': self.award_amount, 'discount': 0,
                'tax_ids': [Command.clear()]})],
        })
        bill.action_post()
        if bill.date != self.service_end or money(bill.amount_total) != money(self.award_amount) or money(bill.amount_tax) != 0:
            raise ValidationError(_('The native bill does not match the reviewed award amount, tax or date.'))
        self.with_context(baseer_payroll_internal=INTERNAL).write({'state': 'approved', 'bill_id': bill.id,
            'approved_by_id': self.env.user.id, 'approved_at': fields.Datetime.now()})
        return self.action_view_bill()

    def action_view_bill(self):
        self.ensure_one()
        self.check_access('read')
        if not self.bill_id:
            raise UserError(_('This estimate has not issued a bill.'))
        return {'type': 'ir.actions.act_window', 'name': _('Award Bill'), 'res_model': 'account.move',
                'res_id': self.bill_id.id, 'view_mode': 'form', 'views': [(self.env.ref('account.view_move_form').id, 'form')]}


class EosEmployee(models.Model):
    _inherit = 'hr.employee'
    baseer_eos_ids = fields.One2many('baseer.hr.eos', 'employee_id', groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_eos_count = fields.Integer(compute='_compute_eos_count', groups='om_hr_payroll.group_hr_payroll_manager', compute_sudo=False)

    def _compute_eos_count(self):
        for employee in self:
            employee.baseer_eos_count = len(employee.baseer_eos_ids)

    def action_view_eos(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('End-of-Service Awards'), 'res_model': 'baseer.hr.eos',
                'view_mode': 'list,form', 'domain': [('employee_id', '=', self.id), ('company_id', '=', self.company_id.id)],
                'context': {'default_employee_id': self.id, 'default_version_id': self.version_id.id}}


class EosBill(models.Model):
    _inherit = 'account.move'
    baseer_eos_id = fields.Many2one('baseer.hr.eos', readonly=True, copy=False, ondelete='restrict', index=True)

    def _eos_protect(self):
        if not internal(self) and self.filtered('baseer_eos_id'):
            raise UserError(_('The award bill amount and source are protected. Use a native reversal for a reviewed correction.'))

    @api.model_create_multi
    def create(self, vals_list):
        default = self.default_get(['baseer_eos_id']).get('baseer_eos_id')
        if not internal(self) and any(vals.get('baseer_eos_id', default) for vals in vals_list):
            raise AccessError(_('Award bill links are created only by award approval.'))
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self) and 'baseer_eos_id' in vals:
            raise AccessError(_('Award bill source links cannot be changed.'))
        if set(vals) & {'partner_id', 'company_id', 'currency_id', 'journal_id', 'invoice_line_ids', 'line_ids',
                        'move_type', 'invoice_date', 'date', 'fiscal_position_id', 'invoice_payment_term_id', 'amount_total', 'amount_untaxed', 'amount_tax'}:
            self._eos_protect()
        return super().write(vals)

    def unlink(self):
        if self.filtered('baseer_eos_id'):
            raise UserError(_('Award bill history cannot be deleted. Native cancellation or reversal does not permit another award for the same service period.'))
        return super().unlink()


class EosBillLine(models.Model):
    _inherit = 'account.move.line'

    @api.model_create_multi
    def create(self, vals_list):
        default = self.default_get(['move_id']).get('move_id')
        self.env['account.move'].browse([vals.get('move_id', default) for vals in vals_list if vals.get('move_id', default)])._eos_protect()
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) & {'move_id', 'partner_id', 'company_id', 'account_id', 'currency_id', 'product_id', 'quantity',
                        'price_unit', 'discount', 'debit', 'credit', 'balance', 'amount_currency', 'tax_ids', 'tax_line_id',
                        'price_subtotal', 'price_total', 'amount_residual', 'amount_residual_currency', 'display_type'}:
            (self.move_id | self.env['account.move'].browse(vals.get('move_id')))._eos_protect()
        return super().write(vals)

    def unlink(self):
        self.move_id._eos_protect()
        return super().unlink()
