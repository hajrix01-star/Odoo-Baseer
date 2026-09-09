"""Native payroll settings backed by the active company's existing configuration."""
from odoo import fields, models


class PayrollSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    baseer_payroll_journal_id = fields.Many2one(related='company_id.baseer_payroll_journal_id', readonly=False)
    baseer_salary_expense_id = fields.Many2one(related='company_id.baseer_salary_expense_id', readonly=False)
    baseer_salary_payable_id = fields.Many2one(related='company_id.baseer_salary_payable_id', readonly=False)
    baseer_deduction_account_id = fields.Many2one(related='company_id.baseer_deduction_account_id', readonly=False)
    baseer_loan_account_id = fields.Many2one(related='company_id.baseer_loan_account_id', readonly=False)
    baseer_proration = fields.Selection(related='company_id.baseer_proration', readonly=False)
