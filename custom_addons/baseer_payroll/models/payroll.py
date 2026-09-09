"""Simple monthly runs on the Odoo Mates engine and native accounting."""
import calendar
from datetime import datetime, time, timedelta
from decimal import Decimal
from pytz import timezone, UTC
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from .common import INTERNAL, decimal, money, validate_money, require_manager, lock_employee, split_salary


class Company(models.Model):
    _inherit = 'res.company'
    baseer_salary_expense_id = fields.Many2one('account.account', string='Salary expense')
    baseer_salary_payable_id = fields.Many2one('account.account', string='Salary payable')
    baseer_deduction_account_id = fields.Many2one('account.account', string='Deduction recovery')
    baseer_loan_account_id = fields.Many2one('account.account', string='Employee advances receivable')
    baseer_payroll_journal_id = fields.Many2one('account.journal', string='Payroll journal')
    baseer_proration = fields.Selection([('calendar','Calendar days'),('fixed30','30-day basis')], default='calendar', required=True, string='Partial-month salary basis')
    baseer_structure_id = fields.Many2one('hr.payroll.structure', copy=False)

    @api.constrains('baseer_salary_expense_id')
    def _check_baseer_salary_expense(self):
        for company in self:
            expense = company.baseer_salary_expense_id
            if expense and (not expense.active or company not in expense.company_ids
                            or expense.account_type not in ('expense', 'expense_direct_cost')):
                raise ValidationError(_('Salary expense must use an active expense or direct-cost account.'))

    def _baseer_check_payroll_configuration(self):
        self.ensure_one()
        if self != self.env.company:
            raise AccessError(_('Switch to the payroll company.'))
        accounts = self.baseer_salary_expense_id | self.baseer_salary_payable_id | self.baseer_deduction_account_id | self.baseer_loan_account_id
        if len(accounts) < 4 or not self.baseer_payroll_journal_id:
            raise UserError(_('Payroll is not configured for company "%(company)s". Open Payroll > Settings and set the payroll journal, salary expense, salary payable, deduction recovery and employee advance accounts.', company=self.display_name))
        if any(self not in a.company_ids for a in accounts) or self.baseer_payroll_journal_id.company_id != self:
            raise ValidationError(_('Payroll accounts and journal must belong to the selected company.'))
        if not self.baseer_salary_expense_id.active or self.baseer_salary_expense_id.account_type not in ('expense', 'expense_direct_cost'):
            raise ValidationError(_('Salary expense must use an active expense or direct-cost account.'))
        if self.baseer_salary_payable_id.account_type != 'liability_payable' or not self.baseer_salary_payable_id.reconcile:
            raise ValidationError(_('Salary payable must be a reconcilable payable account.'))
        if self.baseer_loan_account_id.account_type != 'asset_receivable' or not self.baseer_loan_account_id.reconcile:
            raise ValidationError(_('Employee advances must use a reconcilable receivable account.'))
        if self.baseer_payroll_journal_id.type != 'general':
            raise ValidationError(_('Use a general journal for payroll accruals.'))

    def _baseer_structure(self):
        self._baseer_check_payroll_configuration()
        if not self.baseer_structure_id:
            structure = self.env['hr.payroll.structure'].create({'name':_('Baseer monthly salaries'),'code':'BASEER','company_id':self.id,'parent_id':False})
            self.baseer_structure_id = structure
            category = self.env['hr.salary.rule.category'].create({'name':_('Baseer salaries'),'code':'BASEER','company_id':self.id})
            definitions = [('BASIC','Basic salary','baseer_basic',self.baseer_salary_expense_id,self.baseer_salary_payable_id),('OT','Included overtime','baseer_overtime',self.baseer_salary_expense_id,self.baseer_salary_payable_id),('ALLOW','Allowances','baseer_allowance',self.baseer_salary_expense_id,self.baseer_salary_payable_id),('DEDUCT','Other deductions','baseer_deduction',self.baseer_salary_payable_id,self.baseer_deduction_account_id),('LOAN','Advance installment','baseer_loan_amount',self.baseer_salary_payable_id,self.baseer_loan_account_id)]
            rules = self.env['hr.salary.rule']
            for seq,(code,name,field,debit,credit) in enumerate(definitions,1):
                rules |= rules.create({'name':name,'code':'BP_'+code,'sequence':seq*10,'company_id':self.id,'category_id':category.id,'amount_select':'code','amount_python_compute':'result = payslip.'+field,'account_debit':debit.id,'account_credit':credit.id})
            structure.rule_ids = rules
        expected={
            'BP_BASIC':('baseer_basic',self.baseer_salary_expense_id,self.baseer_salary_payable_id),
            'BP_OT':('baseer_overtime',self.baseer_salary_expense_id,self.baseer_salary_payable_id),
            'BP_ALLOW':('baseer_allowance',self.baseer_salary_expense_id,self.baseer_salary_payable_id),
            'BP_DEDUCT':('baseer_deduction',self.baseer_salary_payable_id,self.baseer_deduction_account_id),
            'BP_LOAN':('baseer_loan_amount',self.baseer_salary_payable_id,self.baseer_loan_account_id),
        }
        structure=self.baseer_structure_id
        if structure.company_id!=self or structure.parent_id or len(structure.rule_ids)!=5:
            raise ValidationError(_('The Baseer salary structure does not match the company payroll configuration.'))
        for code,(field,debit,credit) in expected.items():
            rule=structure.rule_ids.filtered(lambda r:r.code==code)
            if len(rule)!=1 or rule.company_id!=self or rule.account_debit!=debit or rule.account_credit!=credit or rule.amount_select!='code' or rule.amount_python_compute!='result = payslip.'+field or not rule.active:
                raise ValidationError(_('The Baseer salary rules do not match the company payroll configuration. Review the payroll accounts before approving.'))
        return self.baseer_structure_id


class Version(models.Model):
    _inherit = 'hr.version'
    wage = fields.Monetary(groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')
    baseer_salary_mode = fields.Selection([('fixed','Fixed monthly'),('inclusive','Total includes overtime')], default='fixed', required=True, string='Salary calculation', groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')
    baseer_daily_hours = fields.Float(related='resource_calendar_id.hours_per_day', string='Hours per day', readonly=True, store=False, digits=(16, 2), groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')
    baseer_work_days = fields.Integer(default=26, string='Working days per month', groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')
    baseer_allowance_total = fields.Monetary(string='Allowances included in total', groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')
    baseer_basic_salary = fields.Monetary(compute='_compute_baseer_salary', string='Basic salary', groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')
    baseer_overtime_salary = fields.Monetary(compute='_compute_baseer_salary', string='Included overtime', groups='hr.group_hr_manager,om_hr_payroll.group_hr_payroll_manager')

    def _baseer_split(self):
        self.ensure_one()
        return split_salary(self.wage, self.baseer_allowance_total, self.baseer_salary_mode,
                            self.resource_calendar_id.hours_per_day, self.baseer_work_days)

    @api.depends('wage','baseer_salary_mode','baseer_daily_hours','baseer_work_days','baseer_allowance_total')
    def _compute_baseer_salary(self):
        for rec in self:
            try:
                basic,ot,_allow = rec._baseer_split()
            except ValidationError:
                # Display invalid drafts without retaining a misleading previous result.
                # The write constraint and posting path still reject invalid salaries.
                basic,ot = 0,0
            rec.baseer_basic_salary,rec.baseer_overtime_salary = float(basic),float(ot)

    @api.constrains('wage','baseer_salary_mode','resource_calendar_id','baseer_work_days','baseer_allowance_total')
    def _check_baseer_salary(self):
        # Internal validation only: HR can create an incomplete native profile
        # without acquiring read/write access to payroll-restricted fields.
        for rec in self.sudo():
            if rec.employee_id.baseer_payroll_enabled:
                validate_money(rec.wage)
                validate_money(rec.baseer_allowance_total)
                if rec.wage:
                    rec._baseer_split()

    @api.model_create_multi
    def create(self, vals_list):
        if any('baseer_daily_hours' in vals for vals in vals_list):
            raise ValidationError(_('Working hours are managed in the Odoo work schedule.'))
        self.check_access('create')
        employee_ids = [vals.get('employee_id', self.env.context.get('default_employee_id')) for vals in vals_list]
        employee_ids = [employee_id for employee_id in employee_ids if employee_id]
        employees = self.env['hr.employee'].browse(employee_ids)
        employees.check_access('read')
        employees._baseer_lock_profile()
        records = super().create(vals_list)
        records.employee_id._baseer_invalidate_draft_payroll()
        return records

    def write(self, vals):
        if 'baseer_daily_hours' in vals:
            raise ValidationError(_('Working hours are managed in the Odoo work schedule.'))
        relevant = {'employee_id', 'date_version', 'contract_date_start', 'contract_date_end', 'wage',
                    'baseer_salary_mode', 'baseer_allowance_total', 'baseer_work_days', 'resource_calendar_id'} & set(vals)
        employees = self.employee_id
        if relevant:
            self.check_access('write')
            if vals.get('employee_id'):
                target = self.env['hr.employee'].browse(vals['employee_id'])
                target.check_access('read')
                employees |= target
            employees._baseer_lock_profile()
        result = super().write(vals)
        if relevant:
            (employees | self.employee_id)._baseer_invalidate_draft_payroll()
        return result


class Employee(models.Model):
    _inherit = 'hr.employee'
    baseer_payroll_enabled = fields.Boolean(default=True, string='Include in monthly payroll', groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_salary_total = fields.Monetary(related='version_id.wage', readonly=False, string='Total monthly salary', groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_salary_mode = fields.Selection(related='version_id.baseer_salary_mode', readonly=False, store=False)
    baseer_daily_hours = fields.Float(related='version_id.baseer_daily_hours', readonly=True, store=False, digits=(16, 2))
    baseer_work_days = fields.Integer(related='version_id.baseer_work_days', readonly=False, store=False)
    baseer_allowance_total = fields.Monetary(related='version_id.baseer_allowance_total', readonly=False, store=False)
    baseer_work_calendar_id = fields.Many2one(related='version_id.resource_calendar_id', readonly=True, string='Working schedule', groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_basic_salary = fields.Monetary(compute='_compute_baseer_salary_preview', store=False, string='Basic salary')
    baseer_overtime_salary = fields.Monetary(compute='_compute_baseer_salary_preview', store=False, string='Included overtime')
    baseer_salary_warning = fields.Char(compute='_compute_baseer_salary_preview', groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_loan_count = fields.Integer(compute='_compute_baseer_loans', groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_loan_balance = fields.Monetary(compute='_compute_baseer_loans', groups='om_hr_payroll.group_hr_payroll_manager')

    @api.depends('baseer_salary_total', 'baseer_allowance_total', 'baseer_salary_mode',
                 'baseer_daily_hours', 'baseer_work_days')
    def _compute_baseer_salary_preview(self):
        for emp in self:
            warning = False
            try:
                basic, overtime, _allowance = split_salary(emp.baseer_salary_total,
                    emp.baseer_allowance_total, emp.baseer_salary_mode,
                    emp.baseer_daily_hours, emp.baseer_work_days)
            except ValidationError as error:
                basic, overtime, warning = 0, 0, str(error)
            emp.baseer_basic_salary = float(basic)
            emp.baseer_overtime_salary = float(overtime)
            emp.baseer_salary_warning = warning

    @api.constrains('baseer_payroll_enabled', 'baseer_salary_total', 'baseer_allowance_total',
                    'baseer_salary_mode', 'baseer_work_days', 'version_id')
    def _check_baseer_salary_inputs(self):
        for emp in self.sudo().filtered('baseer_payroll_enabled'):
            validate_money(emp.version_id.wage)
            validate_money(emp.version_id.baseer_allowance_total)
            if emp.version_id.wage:
                emp.version_id._baseer_split()

    @api.model_create_multi
    def create(self, vals_list):
        if any('baseer_daily_hours' in vals for vals in vals_list):
            raise ValidationError(_('Working hours are managed in the Odoo work schedule.'))
        if 'default_baseer_payroll_enabled' in self.env.context:
            self._check_field_access(self._fields['baseer_payroll_enabled'], 'write')
        return super().create(vals_list)

    def write(self, vals):
        if 'baseer_daily_hours' in vals:
            raise ValidationError(_('Working hours are managed in the Odoo work schedule.'))
        relevant = {'active', 'company_id', 'version_id', 'baseer_payroll_enabled', 'baseer_salary_total',
                    'baseer_salary_mode', 'baseer_allowance_total', 'baseer_work_days', 'wage',
                    'contract_date_start', 'contract_date_end', 'date_version', 'resource_calendar_id'} & set(vals)
        if relevant:
            self.check_access('write')
            self._baseer_lock_profile()
            if 'company_id' in vals:
                self._baseer_invalidate_draft_payroll()
        result = super().write(vals)
        if relevant:
            self._baseer_invalidate_draft_payroll()
        return result

    def _baseer_lock_profile(self):
        for employee in self.sorted(lambda row: (row.company_id.id, row.id)):
            lock_employee(self.env, employee.id, employee.company_id.id)

    def _baseer_invalidate_draft_payroll(self):
        """After native HR authorization, invalidate only this profile's draft cache.

        The private, narrowly scoped elevation grants no financial read/write
        rights to the caller and never enters an approval/payment operation.
        """
        self._baseer_lock_profile()
        for employee in self:
            drafts = self.env['hr.payslip'].sudo().search([
                ('employee_id', '=', employee.id), ('company_id', '=', employee.company_id.id),
                ('baseer_managed', '=', True), ('state', '=', 'draft'), ('move_id', '=', False)])
            for draft in drafts:
                self.env.cr.execute("UPDATE hr_payslip SET write_date=write_date WHERE id=%s AND state='draft' AND move_id IS NULL RETURNING id", [draft.id])
                if self.env.cr.fetchone():
                    draft.invalidate_recordset()
                    draft._baseer_clear_draft_calculation()

    def _compute_baseer_loans(self):
        for emp in self:
            loans = self.env['baseer.hr.loan'].search([('employee_id','=',emp.id),('company_id','=',emp.company_id.id)])
            emp.baseer_loan_count = len(loans)
            emp.baseer_loan_balance = float(sum((decimal(l.balance) for l in loans),Decimal(0)))

    def action_view_loans(self):
        self.ensure_one()
        return {'type':'ir.actions.act_window','name':_('Employee advances and history'),'res_model':'baseer.hr.loan','view_mode':'list,form','domain':[('employee_id','=',self.id),('company_id','=',self.company_id.id)],'context':{'default_employee_id':self.id}}

    def _baseer_version_for_period(self, first, last):
        self.ensure_one()
        versions=self.env['hr.version'].search([('employee_id','=',self.id),('date_version','<=',last),('contract_date_start','<=',last),'|',('contract_date_end','=',False),('contract_date_end','>=',first)],order='date_version desc')
        if len(versions)>1 and versions.filtered(lambda v:first<v.date_version<=last):
            raise ValidationError(_('A salary version changed during this month. Review the partial-period salary before generating the run.'))
        return versions[:1]

    def _baseer_incomplete_version_for_period(self, first, last):
        """A real missing-start version may explain a draft, never earn salary."""
        self.ensure_one()
        return self.env['hr.version'].search([
            ('employee_id', '=', self.id), ('company_id', '=', self.company_id.id),
            ('date_version', '<=', last), ('contract_date_start', '=', False),
            '|', ('contract_date_end', '=', False), ('contract_date_end', '>=', first),
        ], order='date_version desc, id desc', limit=1)


class LeaveType(models.Model):
    _inherit = 'hr.leave.type'
    baseer_unpaid = fields.Boolean(string='Unpaid for Baseer payroll', help='Approved days of this leave type reduce salary. Other leave types preserve salary.')


class Payslip(models.Model):
    _inherit = 'hr.payslip'
    baseer_managed = fields.Boolean(default=False, copy=False, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    baseer_gross = fields.Monetary(readonly=True, string='Salary before deductions')
    baseer_basic = fields.Monetary(readonly=True, string='Basic salary')
    baseer_overtime = fields.Monetary(readonly=True, string='Included overtime')
    baseer_allowance = fields.Monetary(readonly=True, string='Allowances')
    baseer_days = fields.Float(readonly=True, digits=(16,2), string='Eligible days')
    baseer_month_days = fields.Integer(readonly=True, string='Month days')
    baseer_loan_amount = fields.Monetary(readonly=True, string='Advance deduction')
    baseer_defer_loan = fields.Boolean(string='Defer installments')
    baseer_deduction = fields.Monetary(string='Other deductions')
    baseer_deduction_reason = fields.Char(string='Deduction reason')
    baseer_needs_refresh = fields.Boolean(readonly=True, copy=False)
    baseer_readiness_warning = fields.Char(compute='_compute_baseer_readiness', string='Setup warning')
    baseer_readiness = fields.Selection([('ready', 'Ready'), ('pending', 'Needs setup')],
        compute='_compute_baseer_readiness', string='Payroll readiness')
    baseer_net = fields.Monetary(compute='_compute_baseer_totals', store=True, string='Net salary')
    baseer_original_net = fields.Monetary(compute='_compute_baseer_totals', store=True, string='Original net salary')
    baseer_adjustment = fields.Monetary(compute='_compute_baseer_totals', store=True, string='Posted adjustments')
    baseer_correction_ids = fields.One2many('account.move', 'baseer_correction_payslip_id', readonly=True)
    baseer_paid = fields.Monetary(compute='_compute_baseer_payment', string='Paid')
    baseer_pending = fields.Monetary(compute='_compute_baseer_payment', string='Payments in process')
    baseer_settled = fields.Monetary(compute='_compute_baseer_payment', string='Liability settled')
    baseer_residual = fields.Monetary(compute='_compute_baseer_payment', string='Remaining salary')

    @api.depends('baseer_gross','baseer_loan_amount','baseer_deduction', 'baseer_needs_refresh', 'state', 'baseer_correction_ids.state',
                 'baseer_correction_ids.line_ids.balance', 'baseer_correction_ids.line_ids.account_id')
    def _compute_baseer_totals(self):
        for rec in self:
            if rec.baseer_managed and rec.state == 'draft' and rec.baseer_needs_refresh:
                rec.baseer_original_net, rec.baseer_adjustment, rec.baseer_net = 0, 0, 0
                continue
            original = money(decimal(rec.baseer_gross)-decimal(rec.baseer_loan_amount)-decimal(rec.baseer_deduction))
            adjustment = -sum((decimal(line.balance) for line in rec.baseer_correction_ids.filtered(
                lambda move: move.state == 'posted').line_ids.filtered(lambda line: line.account_id.account_type == 'liability_payable')), Decimal(0))
            rec.baseer_original_net, rec.baseer_adjustment = float(original), float(money(adjustment))
            rec.baseer_net = float(money(original + adjustment))

    def _baseer_setup_problem(self):
        self.ensure_one()
        self.check_access('read')
        # Only the generic setup status crosses the HR field boundary. The
        # caller keeps native payslip/report access and all posting checks.
        self = self.sudo()
        if not self.baseer_managed or self.state != 'draft':
            return False
        if not self.employee_id.active or not self.employee_id.baseer_payroll_enabled:
            return _('This employee is not included in payroll. Refresh employees to review this row.')
        version = self.version_id
        if not version or not version.contract_date_start:
            return _('Enter the employee contract start date, then refresh employees. No salary or deductions have been calculated.')
        if version.contract_date_start > self.date_to or (version.contract_date_end and version.contract_date_end < self.date_from):
            return _('The employee contract does not cover this month. Refresh employees to review this row.')
        if not version.wage:
            return _('Enter the employee monthly salary, then refresh employees. No salary or deductions have been calculated.')
        if version != self.employee_id._baseer_version_for_period(self.date_from, self.date_to):
            return _('The employee salary version changed. Refresh the payroll run before approval.')
        return False

    @api.depends_context('lang')
    @api.depends('baseer_needs_refresh', 'state', 'version_id', 'version_id.contract_date_start',
                 'version_id.contract_date_end', 'version_id.wage', 'employee_id.active', 'employee_id.baseer_payroll_enabled')
    def _compute_baseer_readiness(self):
        for slip in self:
            warning = slip._baseer_setup_problem()
            if not warning and slip.baseer_managed and slip.state == 'draft' and slip.baseer_needs_refresh:
                warning = _('Employee information changed. Refresh employees to calculate and review this salary.')
            slip.baseer_readiness_warning = warning
            slip.baseer_readiness = 'pending' if warning else 'ready'

    def _baseer_clear_draft_calculation(self):
        """Clear only derived draft values; manual deduction/defer inputs survive."""
        drafts = self.filtered(lambda slip: slip.baseer_managed and slip.state == 'draft' and not slip.move_id)
        for slip in drafts:
            guarded = slip.with_context(baseer_payroll_internal=INTERNAL)
            guarded.line_ids.unlink()
            guarded.write({'baseer_needs_refresh': True, 'baseer_gross': 0, 'baseer_basic': 0,
                'baseer_overtime': 0, 'baseer_allowance': 0, 'baseer_days': 0, 'baseer_month_days': 0,
                'baseer_loan_amount': 0})

    def _baseer_assert_ready(self):
        for slip in self.filtered(lambda row: row.baseer_managed and row.state == 'draft'):
            slip._baseer_company_check()
            problem = slip._baseer_setup_problem()
            if problem or slip.baseer_needs_refresh:
                raise ValidationError(_('%(employee)s: %(reason)s', employee=slip.employee_id.name,
                    reason=problem or _('Employee information changed. Refresh employees to calculate and review this salary.')))

    @api.depends('move_id.line_ids.amount_residual', 'move_id.state', 'baseer_net',
                 'move_id.line_ids.matched_debit_ids.debit_move_id.move_id.origin_payment_id.state',
                 'move_id.line_ids.matched_debit_ids.debit_move_id.move_id.origin_payment_id.is_matched',
                 'baseer_correction_ids.line_ids.amount_residual', 'baseer_correction_ids.state',
                 'baseer_correction_ids.line_ids.matched_debit_ids.debit_move_id.move_id.origin_payment_id.state',
                 'baseer_correction_ids.line_ids.matched_debit_ids.debit_move_id.move_id.origin_payment_id.is_matched')
    def _compute_baseer_payment(self):
        for rec in self:
            if not rec.move_id:
                rec.baseer_residual = rec.baseer_net
                rec.baseer_paid = rec.baseer_pending = rec.baseer_settled = 0
                continue
            payable = rec._baseer_accounting_moves().line_ids.filtered(lambda l:l.account_id.account_type=='liability_payable')
            residual = max(Decimal(0),-sum((decimal(l.amount_residual) for l in payable),Decimal(0))) if rec.move_id else decimal(rec.baseer_net)
            rec.baseer_residual = float(money(residual))
            rec.baseer_settled = float(money(decimal(rec.baseer_net)-residual)) if rec.move_id else 0
            rows = rec._baseer_payment_allocations() if rec.move_id else []
            rec.baseer_paid = float(money(sum((row['amount'] for row in rows if row['confirmed']), Decimal(0))))
            rec.baseer_pending = float(money(sum((row['amount'] for row in rows if not row['confirmed']), Decimal(0))))

    def _lock(self):
        for rec in self.sorted(lambda s:(s.company_id.id,s.employee_id.id,s.id)):
            require_manager(rec)
            lock_employee(self.env,rec.employee_id.id,rec.company_id.id)
            # A new row version makes a waiting REPEATABLE READ writer retry its
            # transaction instead of accepting evidence from its old snapshot.
            self.env.cr.execute('UPDATE hr_payslip SET write_date=write_date WHERE id=%s', [rec.id])
        self.invalidate_recordset()

    @api.model_create_multi
    def create(self, vals_list):
        internal = self.env.context.get('baseer_payroll_internal') is INTERNAL
        for vals in vals_list:
            if not internal:
                keys=['state','move_id','paid','baseer_managed','baseer_gross','baseer_basic','baseer_overtime','baseer_allowance','baseer_days','baseer_month_days','baseer_loan_amount','baseer_net','baseer_needs_refresh','baseer_readiness','baseer_readiness_warning']
                effective={**self.default_get(keys),**vals}
                if effective.get('state','draft')!='draft' or any(effective.get(k) for k in keys if k!='state'):
                    raise AccessError(_('Payroll financial fields are set by the approved workflow.'))
        records = super().create(vals_list)
        records._baseer_company_check()
        return records

    def _baseer_company_check(self):
        for rec in self:
            if rec.employee_id.company_id != rec.company_id or (rec.version_id and (rec.version_id.employee_id!=rec.employee_id or rec.version_id.company_id!=rec.company_id)) or (rec.journal_id and rec.journal_id.company_id!=rec.company_id) or (rec.struct_id and rec.struct_id.company_id!=rec.company_id) or (rec.payslip_run_id and rec.payslip_run_id.company_id!=rec.company_id):
                raise ValidationError(_('Employee, contract, structure, journal and run must belong to the same company.'))

    def write(self, vals):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            self._lock()
            protected={'state','move_id','paid','baseer_managed','baseer_gross','baseer_basic','baseer_overtime','baseer_allowance','baseer_days','baseer_month_days','baseer_loan_amount','baseer_net','baseer_needs_refresh','baseer_readiness','baseer_readiness_warning'}
            if protected.intersection(vals):
                raise AccessError(_('Payroll financial fields are set by the approved workflow.'))
            if any(s.state=='done' or s.move_id for s in self) and set(vals)-{'message_follower_ids','message_ids','activity_ids'}:
                raise UserError(_('Posted payroll is protected. Corrections require a reviewed reversal; it cannot be edited or cancelled.'))
            if 'baseer_deduction' in vals:
                validate_money(vals['baseer_deduction'])
        result=super().write(vals)
        self._baseer_company_check()
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL and {'employee_id','version_id','date_from','date_to','payslip_run_id'}.intersection(vals):
            self._baseer_clear_draft_calculation()
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL and {'baseer_deduction','baseer_defer_loan'}.intersection(vals):
            for slip in self.filtered('baseer_managed'):
                if slip._baseer_setup_problem() or slip.baseer_needs_refresh:
                    slip._baseer_clear_draft_calculation()
                else:
                    slip._refresh_amounts()
        return result

    def unlink(self):
        self._lock()
        if any(s.state=='done' or s.move_id for s in self):
            raise UserError(_('Posted payroll cannot be deleted.'))
        return super(Payslip,self.with_context(baseer_payroll_internal=INTERNAL)).unlink()

    def _refresh_amounts(self):
        for slip in self:
            if slip.state!='draft':
                continue
            version = slip.version_id
            if slip.baseer_managed:
                problem = slip._baseer_setup_problem()
                if problem:
                    raise ValidationError(problem)
                run=slip.payslip_run_id
                if not run or slip.date_from!=run.date_start or slip.date_to!=run.date_end:
                    raise ValidationError(_('The payslip period must match its monthly payroll run.'))
                if version!=slip.employee_id._baseer_version_for_period(slip.date_from,slip.date_to):
                    raise ValidationError(_('The employee salary version changed. Refresh the payroll run before approval.'))
            version._baseer_split()
            first,last=slip.date_from,slip.date_to
            start=max(first,version.contract_date_start or first)
            end=min(last,version.contract_date_end or last)
            dates={start+timedelta(days=i) for i in range(max(0,(end-start).days+1))}
            leaves=self.env['hr.leave'].search([('employee_id','=',slip.employee_id.id),('state','=','validate'),('holiday_status_id.baseer_unpaid','=',True),('request_date_from','<=',last),('request_date_to','>=',first)])
            absence = {day: Decimal(0) for day in dates}
            for leave in leaves:
                affected = sorted(day for day in dates if leave.request_date_from <= day <= leave.request_date_to)
                for day in affected:
                    if not (leave.request_unit_half or leave.request_unit_hours):
                        portion = Decimal(1)
                    elif leave.request_date_from == leave.request_date_to:
                        portion = decimal(leave.number_of_days)
                    else:
                        # Use Odoo's approved hours and calendar to apportion a partial
                        # leave across payroll/contract boundaries, without editing it.
                        tz = timezone(leave.tz or 'UTC')
                        lower = tz.localize(datetime.combine(day, time.min)).astimezone(UTC).replace(tzinfo=None)
                        upper = tz.localize(datetime.combine(day + timedelta(days=1), time.min)).astimezone(UTC).replace(tzinfo=None)
                        data = leave.employee_id._get_work_days_data_batch(
                            max(lower, leave.date_from), min(upper, leave.date_to),
                            compute_leaves=not leave.holiday_status_id.include_public_holidays_in_duration,
                            calendar=leave.resource_calendar_id,
                            domain=[('time_type', '=', 'leave'), ('company_id', '=', slip.company_id.id),
                                    ('holiday_id', '!=', leave.id)])[leave.employee_id.id]
                        portion = (decimal(data['hours']) * decimal(leave.number_of_days)
                                   / decimal(leave.number_of_hours)) if leave.number_of_hours else Decimal(0)
                    absence[day] = min(Decimal(1), absence[day] + portion)
            unpaid = sum(absence.values(), Decimal(0))
            days = Decimal(len(dates)) - unpaid
            denom=30 if slip.company_id.baseer_proration=='fixed30' else calendar.monthrange(first.year,first.month)[1]
            if slip.company_id.baseer_proration == 'fixed30':
                covered = Decimal(1) if start == first and end == last else min(Decimal(1), Decimal(len(dates)) / 30)
                ratio = max(Decimal(0), covered - unpaid / 30)
            else:
                ratio=min(Decimal(1), days / Decimal(denom))
            gross=money(decimal(version.wage)*ratio)
            b,o,a=version._baseer_split()
            basic,allowance=money(b*ratio),money(a*ratio)
            if version.baseer_salary_mode=='fixed':
                overtime=money(0)
                basic=money(gross-allowance)
            else:
                overtime=money(gross-basic-allowance)
            loans=self.env['baseer.hr.loan'].search([('employee_id','=',slip.employee_id.id),('company_id','=',slip.company_id.id),('state','=','running')])
            loan=money(0 if slip.baseer_defer_loan or not days else loans._due_amount(last))
            deduction=validate_money(slip.baseer_deduction)
            if loan+deduction>gross:
                raise ValidationError(_('Deductions exceed the salary. Defer installments or adjust the deduction.'))
            slip.with_context(baseer_payroll_internal=INTERNAL).write({'baseer_needs_refresh':False,'baseer_gross':float(gross),'baseer_basic':float(basic),'baseer_allowance':float(allowance),'baseer_overtime':float(overtime),'baseer_days':days,'baseer_month_days':denom,'baseer_loan_amount':float(loan)})

    def compute_sheet(self):
        self._lock()
        if any(s.state!='draft' or s.move_id for s in self):
            raise UserError(_('Only draft payslips can be recalculated.'))
        self.filtered('baseer_managed')._refresh_amounts()
        return super(Payslip,self.with_context(baseer_payroll_internal=INTERNAL)).compute_sheet()

    def action_payslip_done(self):
        self._lock()
        self._baseer_assert_ready()
        if not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_('Accounting access is required to approve payroll.'))
        for slip in self:
            if slip.state=='done' and slip.move_id.state=='posted':
                continue
            if slip.state!='draft' or slip.move_id:
                raise UserError(_('Only an unposted draft can be approved.'))
            slip._baseer_company_check()
            posting_date=slip.date or slip.date_to
            if slip.baseer_managed:
                if posting_date!=slip.date_to:
                    raise ValidationError(_('Monthly payroll must be posted on the last day of its period.'))
                if slip.struct_id!=slip.company_id._baseer_structure():
                    raise ValidationError(_('Use the company Baseer salary structure.'))
            if slip.company_id._get_violated_lock_dates(posting_date,False,slip.journal_id):
                raise ValidationError(_('The payroll accounting date is locked.'))
            existing=self.search([('id','!=',slip.id),('employee_id','=',slip.employee_id.id),('company_id','=',slip.company_id.id),('state','=','done'),('date_from','<=',slip.date_to),('date_to','>=',slip.date_from)],limit=1)
            if existing:
                raise ValidationError(_('This employee already has an approved payslip covering this period.'))
            if slip.baseer_deduction and not slip.baseer_deduction_reason:
                raise ValidationError(_('Enter a reason for the other deduction.'))
            super(Payslip,slip.with_context(baseer_payroll_internal=INTERNAL)).action_payslip_done()
            if slip.move_id.date!=posting_date:
                raise ValidationError(_('The accounting date changed. Review the payroll period and lock dates.'))
            slip.move_id.with_context(baseer_payroll_internal=INTERNAL).write({'baseer_payslip_id':slip.id})
            if slip.baseer_managed:
                loans=self.env['baseer.hr.loan'].search([('employee_id','=',slip.employee_id.id),('company_id','=',slip.company_id.id),('state','=','running')],order='date,id')
                if slip.baseer_defer_loan:
                    loans._defer_due(slip.date_to,_('Deferred from payroll %s') % slip.name)
                elif slip.baseer_loan_amount:
                    loans._apply_payroll(slip,slip.move_id)
                payable=slip.move_id.line_ids.filtered(lambda l:l.account_id==slip.company_id.baseer_salary_payable_id)
                if money(-sum((decimal(l.balance) for l in payable),Decimal(0)))!=money(slip.baseer_net):
                    raise ValidationError(_('Payroll liability does not match the net salary.'))
                # Settle debit deductions against gross credit before cash payments.
                if payable.filtered(lambda l:l.debit) and payable.filtered(lambda l:l.credit):
                    payable.reconcile()
        return True

    def action_payslip_cancel(self):
        self._lock()
        if any(s.state=='done' or s.move_id for s in self):
            raise UserError(_('Posted payroll is protected. Corrections require a reviewed reversal; it cannot be edited or cancelled.'))
        return self.with_context(baseer_payroll_internal=INTERNAL).write({'state':'cancel'})

    def action_payslip_draft(self):
        self._lock()
        if any(s.move_id for s in self):
            raise UserError(_('Posted payroll cannot be reset.'))
        return self.with_context(baseer_payroll_internal=INTERNAL).write({'state':'draft'})

    def refund_sheet(self):
        raise UserError(_('Use a reviewed payroll correction. Automatic payroll refunds are disabled.'))

    def action_pay(self):
        self._lock()
        if any(s.state!='done' for s in self):
            raise UserError(_('Approve payroll before recording a payment.'))
        lines=self._baseer_accounting_moves().line_ids.filtered(lambda l:l.account_id.account_type=='liability_payable' and not l.reconciled and l.amount_residual)
        if not lines:
            raise UserError(_('These salaries are already fully paid.'))
        return {'type':'ir.actions.act_window','name':_('Pay salaries'),'res_model':'account.payment.register','view_mode':'form','target':'new','context':{'active_model':'account.move.line','active_ids':lines.ids,'default_payment_difference_handling':'open'}}

    def action_view_move(self):
        self.ensure_one()
        return {'type':'ir.actions.act_window','res_model':'account.move','res_id':self.move_id.id,'view_mode':'form'}


class PayslipLine(models.Model):
    _inherit='hr.payslip.line'
    def _get_partner_id(self, credit_account):
        self.ensure_one()
        return self.slip_id.employee_id.work_contact_id.id if self.slip_id.baseer_managed else super()._get_partner_id(credit_account)

    @api.model_create_multi
    def create(self,vals_list):
        for vals in vals_list:
            slip=self.env['hr.payslip'].browse(vals.get('slip_id',self.env.context.get('default_slip_id')))
            if slip:
                slip._lock()
                if (slip.state!='draft' or slip.baseer_managed) and self.env.context.get('baseer_payroll_internal') is not INTERNAL:
                    raise AccessError(_('Calculated payroll lines are protected.'))
        return super().create(vals_list)
    def write(self,vals):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            slips=self.slip_id | self.env['hr.payslip'].browse(vals.get('slip_id'))
            slips._lock()
            if any(s.state!='draft' or s.baseer_managed for s in slips):
                raise AccessError(_('Calculated payroll lines are protected.'))
        return super().write(vals)
    def unlink(self):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            self.slip_id._lock()
            if any(s.state!='draft' or s.baseer_managed for s in self.slip_id):
                raise AccessError(_('Calculated payroll lines are protected.'))
        return super().unlink()


class Run(models.Model):
    _inherit='hr.payslip.run'
    state=fields.Selection(selection_add=[('draft','Draft'),('done','Approved'),('close','Closed')])
    company_id=fields.Many2one('res.company',required=True,default=lambda self:self.env.company,index=True)
    currency_id=fields.Many2one(related='company_id.currency_id')
    baseer_managed=fields.Boolean(default=False,index=True)
    baseer_month=fields.Date(default=lambda self:fields.Date.today().replace(day=1),string='Payroll month')
    baseer_gross=fields.Monetary(compute='_totals',string='Total salaries')
    baseer_deduction=fields.Monetary(compute='_totals',string='Other deductions')
    baseer_loan_amount=fields.Monetary(compute='_totals',string='Advance deductions')
    baseer_net=fields.Monetary(compute='_totals',string='Net salaries')
    baseer_paid=fields.Monetary(compute='_totals',string='Paid')
    baseer_pending=fields.Monetary(compute='_totals',string='Payments in process')
    baseer_settled=fields.Monetary(compute='_totals',string='Liability settled')
    baseer_residual=fields.Monetary(compute='_totals',string='Remaining')
    baseer_pending_setup_count=fields.Integer(compute='_totals', string='Employees requiring setup')

    @api.depends('slip_ids.baseer_gross','slip_ids.baseer_deduction','slip_ids.baseer_loan_amount','slip_ids.baseer_net','slip_ids.baseer_paid','slip_ids.baseer_residual','slip_ids.baseer_pending','slip_ids.baseer_settled','slip_ids.baseer_readiness','slip_ids.state')
    def _totals(self):
        for run in self:
            pending=run.slip_ids.filtered(lambda s:s.state=='draft' and s.baseer_readiness=='pending')
            run.baseer_pending_setup_count=len(pending)
            for field in ['baseer_gross','baseer_deduction','baseer_loan_amount','baseer_net','baseer_paid','baseer_residual','baseer_pending','baseer_settled']:
                run[field]=float(money(sum((decimal(s[field]) for s in (run.slip_ids-pending).filtered(lambda s:s.state!='cancel')),Decimal(0))))

    @api.model_create_multi
    def create(self,vals_list):
        for vals in vals_list:
            if vals.get('state',self.env.context.get('default_state','draft'))!='draft':
                raise AccessError(_('Create a draft payroll first.'))
            managed=vals.get('baseer_managed',self.env.context.get('default_baseer_managed',False))
            if managed:
                month=fields.Date.to_date(vals.get('baseer_month') or fields.Date.today()).replace(day=1)
                company=self.env.company
                if vals.get('company_id',company.id)!=company.id:
                    raise AccessError(_('The run belongs to the active company.'))
                company._baseer_check_payroll_configuration()
                vals.update(baseer_managed=True,company_id=company.id,baseer_month=month,date_start=month,date_end=month.replace(day=calendar.monthrange(month.year,month.month)[1]),name=_('Payroll %s') % month.strftime('%Y-%m'),journal_id=company.baseer_payroll_journal_id.id)
        runs=super().create(vals_list)
        runs.filtered('baseer_managed').action_load_employees()
        return runs

    def write(self,vals):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            require_manager(self)
            if 'state' in vals or any(r.state!='draft' for r in self):
                raise AccessError(_('Approved payroll runs are protected.'))
            if any(r.slip_ids for r in self) and {'baseer_month','date_start','date_end','company_id','journal_id','baseer_managed'}.intersection(vals):
                raise UserError(_('Create a new run to change the month or company.'))
        return super().write(vals)

    def action_load_employees(self):
        for run in self:
            require_manager(run)
            self.env.cr.execute('UPDATE hr_payslip_run SET write_date=write_date WHERE id=%s',(run.id,))
            run.invalidate_recordset()
            if run.state!='draft': raise UserError(_('Only draft payroll can be refreshed.'))
            structure=run.company_id._baseer_structure()
            employees=self.env['hr.employee'].search([('active','=',True),('company_id','=',run.company_id.id),('baseer_payroll_enabled','=',True)],order='id')
            for obsolete in run.slip_ids.filtered(lambda s:s.employee_id not in employees):
                obsolete._lock()
                obsolete._baseer_clear_draft_calculation()
                if not (obsolete.baseer_deduction or obsolete.baseer_defer_loan or obsolete.input_line_ids):
                    obsolete.unlink()
            for employee in employees:
                lock_employee(self.env,employee.id,run.company_id.id)
                version=employee._baseer_version_for_period(run.date_start,run.date_end)
                if not version:
                    version=employee._baseer_incomplete_version_for_period(run.date_start,run.date_end)
                existing=run.slip_ids.filtered(lambda s:s.employee_id==employee)
                if not version:
                    if existing:
                        existing._lock()
                        existing._baseer_clear_draft_calculation()
                        if not (existing.baseer_deduction or existing.baseer_defer_loan or existing.input_line_ids):
                            existing.unlink()
                    continue
                if existing:
                    existing._lock()
                    existing.with_context(baseer_payroll_internal=INTERNAL).write({'version_id':version.id})
                    if existing._baseer_setup_problem():
                        existing._baseer_clear_draft_calculation()
                    else:
                        existing.compute_sheet()
                    continue
                if self.env['hr.payslip'].search_count([('employee_id','=',employee.id),('company_id','=',run.company_id.id),('state','!=','cancel'),('date_from','<=',run.date_end),('date_to','>=',run.date_start)]):
                    raise ValidationError(_('An employee already has a payslip for this month. Open the existing run.'))
                slip=self.env['hr.payslip'].with_context(baseer_payroll_internal=INTERNAL).create({'baseer_managed':True,'company_id':run.company_id.id,'employee_id':employee.id,'version_id':version.id,'struct_id':structure.id,'journal_id':run.journal_id.id,'date_from':run.date_start,'date_to':run.date_end,'name':run.name+' / '+employee.name,'payslip_run_id':run.id})
                if slip._baseer_setup_problem():
                    slip._baseer_clear_draft_calculation()
                else:
                    slip.compute_sheet()
        return True

    def unlink(self):
        require_manager(self)
        self.slip_ids._lock()
        if any(r.state!='draft' or r.slip_ids.move_id for r in self):
            raise UserError(_('Approved payroll cannot be deleted.'))
        self.slip_ids.unlink()
        return super().unlink()

    def action_approve(self):
        for run in self:
            require_manager(run)
            self.env.cr.execute('SELECT id FROM hr_payslip_run WHERE id=%s FOR UPDATE',(run.id,))
            run.invalidate_recordset()
            if run.state=='done': continue
            if run.state!='draft' or not run.slip_ids:
                raise UserError(_('Add eligible employees before approving the payroll.'))
            run.slip_ids._lock()
            run.slip_ids._baseer_assert_ready()
            run.slip_ids.action_payslip_done()
            run.with_context(baseer_payroll_internal=INTERNAL).write({'state':'done'})
        return True
    def done_payslip_run(self): return self.action_approve()
    def draft_payslip_run(self):
        raise UserError(_('Approved payroll cannot be reset; use a reviewed correction.'))
    def close_payslip_run(self):
        raise UserError(_('Payroll completion follows its accounting payments.'))
    def action_pay(self): return self.slip_ids.action_pay()
    def action_print(self):
        if not self.slip_ids: raise UserError(_('There are no payslips to print.'))
        self.slip_ids._baseer_assert_ready()
        return self.env.ref('baseer_payroll.action_report_baseer_payslips').report_action(self.slip_ids)


class AccountingMove(models.Model):
    _inherit='account.move'
    baseer_payslip_id=fields.Many2one('hr.payslip',copy=False,index=True,ondelete='restrict')
    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL and any(v.get('baseer_payslip_id',self.env.context.get('default_baseer_payslip_id')) for v in vals_list):
            raise AccessError(_('Payroll source links are set by the payroll workflow.'))
        return super().create(vals_list)
    def _baseer_protect(self):
        if self.filtered('baseer_payslip_id') and self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            raise UserError(_('Payroll accounting entries are protected. Use the payroll workflow.'))
    def button_draft(self): self._baseer_protect(); return super().button_draft()
    def button_cancel(self): self._baseer_protect(); return super().button_cancel()
    def unlink(self): self._baseer_protect(); return super().unlink()
    def _reverse_moves(self,*args,**kwargs): self._baseer_protect(); return super()._reverse_moves(*args,**kwargs)
    def write(self,vals):
        if 'baseer_payslip_id' in vals and self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            raise AccessError(_('Payroll source links are set by the payroll workflow.'))
        if {'date','journal_id','line_ids','state','baseer_payslip_id','company_id','currency_id','partner_id','move_type'}.intersection(vals): self._baseer_protect()
        return super().write(vals)


class AccountingLine(models.Model):
    _inherit='account.move.line'
    def write(self,vals):
        if {'balance','debit','credit','amount_currency','currency_id','account_id','partner_id','move_id','date_maturity','amount_residual','amount_residual_currency','company_id','matched_debit_ids','matched_credit_ids'}.intersection(vals):
            (self.move_id | self.env['account.move'].browse(vals.get('move_id')))._baseer_protect()
        return super().write(vals)
    def unlink(self): self.move_id._baseer_protect(); return super().unlink()
    @api.model_create_multi
    def create(self,vals_list):
        self.env['account.move'].browse([v.get('move_id',self.env.context.get('default_move_id')) for v in vals_list if v.get('move_id',self.env.context.get('default_move_id'))])._baseer_protect()
        return super().create(vals_list)


class CreatePayroll(models.TransientModel):
    _name='baseer.payroll.create'
    _description='Create Monthly Payroll'
    company_id=fields.Many2one('res.company', default=lambda self:self.env.company, required=True, readonly=True)
    baseer_month=fields.Date(default=lambda self:fields.Date.today().replace(day=1), required=True, string='Payroll month')
    def action_create(self):
        self.ensure_one()
        require_manager(self)
        run=self.env['hr.payslip.run'].create({'baseer_managed':True,'baseer_month':self.baseer_month,'company_id':self.company_id.id})
        return {'type':'ir.actions.act_window','name':run.name,'res_model':'hr.payslip.run','res_id':run.id,'view_mode':'form','views':[(self.env.ref('baseer_payroll.view_baseer_payroll_run_form').id,'form')]}


class PayrollPartial(models.Model):
    _inherit='account.partial.reconcile'
    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            for vals in vals_list:
                vals={**self.default_get(['debit_move_id','credit_move_id','amount']),**vals}
                lines=self.env['account.move.line'].browse([vals[k] for k in ('debit_move_id','credit_move_id') if vals.get(k)])
                slips=lines.move_id.baseer_payslip_id
                if slips:
                    slips._lock()
                    lines.invalidate_recordset()
                    if len(lines.account_id)!=1 or len(lines.company_id)!=1 or lines.account_id.account_type!='liability_payable':
                        raise ValidationError(_('Salary payments must reconcile the employee payable account.'))
                    amount=money(vals.get('amount'))
                    if amount<=0 or any(amount>abs(money(l.amount_residual)) for l in lines):
                        raise ValidationError(_('The payment exceeds the remaining salary. Refresh the payment.'))
        return super().create(vals_list)
    def write(self, vals):
        target=self.env['account.move.line'].browse([vals[k] for k in ('debit_move_id','credit_move_id') if vals.get(k)])
        if {'amount','debit_amount_currency','credit_amount_currency','debit_move_id','credit_move_id','company_id'}.intersection(vals) and (self.debit_move_id | self.credit_move_id | target).move_id.baseer_payslip_id:
            raise UserError(_('Salary reconciliations cannot be changed manually.'))
        return super().write(vals)
    def unlink(self):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL and (self.debit_move_id | self.credit_move_id).move_id.baseer_payslip_id:
            raise UserError(_('Salary payment reconciliation cannot be removed independently of payroll.'))
        return super().unlink()
