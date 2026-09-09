from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from .common import INTERNAL


class PayrollInputGuard(models.AbstractModel):
    _name='baseer.payroll.input.guard'
    _description='Protect calculated payroll inputs'

    def _guard_slips(self, extra=None):
        if self.env.context.get('baseer_payroll_internal') is INTERNAL:
            return
        slips=self.payslip_id | self.env['hr.payslip'].browse(extra)
        slips._lock()
        if any(s.baseer_managed or s.state!='draft' or s.move_id for s in slips):
            raise AccessError(_('Calculated payroll inputs are protected.'))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._guard_slips(vals.get('payslip_id',self.env.context.get('default_payslip_id')))
        return super().create(vals_list)
    def write(self, vals):
        self._guard_slips(vals.get('payslip_id'))
        return super().write(vals)
    def unlink(self):
        self._guard_slips()
        return super().unlink()

class PayrollInput(models.Model):
    _name='hr.payslip.input'
    _inherit=['hr.payslip.input','baseer.payroll.input.guard']

class PayrollWorkedDays(models.Model):
    _name='hr.payslip.worked_days'
    _inherit=['hr.payslip.worked_days','baseer.payroll.input.guard']

class EmployeeSecurity(models.Model):
    _inherit='hr.employee'
    baseer_salary_mode=fields.Selection(groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_daily_hours=fields.Float(groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_work_days=fields.Integer(groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_allowance_total=fields.Monetary(groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_basic_salary=fields.Monetary(groups='om_hr_payroll.group_hr_payroll_manager')
    baseer_overtime_salary=fields.Monetary(groups='om_hr_payroll.group_hr_payroll_manager')
    payslip_count=fields.Integer(groups='om_hr_payroll.group_hr_payroll_user')


class SalaryPaymentRegister(models.TransientModel):
    _inherit='account.payment.register'
    baseer_salary_payment_ids=fields.Many2many('account.payment',readonly=True,copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL and any(v.get('baseer_salary_payment_ids',self.env.context.get('default_baseer_salary_payment_ids')) for v in vals_list):
            raise AccessError(_('Salary payment results are set by the payment workflow.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('baseer_payroll_internal') is not INTERNAL:
            if 'baseer_salary_payment_ids' in vals:
                raise AccessError(_('Salary payment results are set by the payment workflow.'))
            if self.baseer_salary_payment_ids:
                raise UserError(_('This salary payment has already been recorded. Open a new payment for the remaining balance.'))
        return super().write(vals)

    def _create_payments(self):
        self.ensure_one()
        slips=self.line_ids.move_id.baseer_payslip_id
        if not slips:
            return super()._create_payments()
        slips._lock()
        self.invalidate_recordset()
        if self.baseer_salary_payment_ids:
            return self.baseer_salary_payment_ids
        payments=super()._create_payments()
        self.with_context(baseer_payroll_internal=INTERNAL).write({'baseer_salary_payment_ids':[(6,0,payments.ids)]})
        return payments
