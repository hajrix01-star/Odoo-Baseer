"""Read-only payroll stages/history derived from native payable reconciliations."""
from collections import defaultdict
from decimal import Decimal
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from .common import money, decimal, require_manager


STAGES = [('draft', 'Draft'), ('awaiting', 'Awaiting Payment'),
          ('partial', 'Partially Paid'), ('processing', 'Payment in process'),
          ('settled', 'Liability settled'), ('paid', 'Fully Paid'), ('cancel', 'Canceled')]


class PayslipReport(models.AbstractModel):
    _name = 'report.baseer_payroll.report_payslips'
    _description = 'Employee payslip report'

    def _get_report_values(self, docids, data=None):
        slips = self.env['hr.payslip'].browse(docids)
        slips.check_access('read')
        slips._baseer_assert_ready()
        return {'doc_ids': slips.ids, 'doc_model': 'hr.payslip', 'docs': slips}


def stage(state, net, remaining, paid=0, pending=0):
    if state == 'cancel':
        return 'cancel'
    if state not in ('done', 'close'):
        return 'draft'
    if money(net) == 0 or (money(remaining) <= 0 and money(paid) >= money(net)):
        return 'paid'
    if money(pending) > 0:
        return 'processing'
    if money(remaining) <= 0:
        return 'settled'
    return 'partial' if money(paid) > 0 else 'awaiting'


def payments_action(payments):
    return {'type': 'ir.actions.act_window', 'name': _('Payment History'),
            'res_model': 'account.payment', 'view_mode': 'list,form',
            'domain': [('id', 'in', payments.ids)], 'context': {'create': False}}


class FinancialPayslip(models.Model):
    _inherit = 'hr.payslip'

    baseer_payment_state = fields.Selection(STAGES, compute='_compute_payment_stage', string='Payment Status')

    @api.depends('state', 'baseer_net', 'baseer_residual', 'baseer_paid', 'baseer_pending')
    def _compute_payment_stage(self):
        for slip in self:
            slip.baseer_payment_state = stage(slip.state, slip.baseer_net, slip.baseer_residual, slip.baseer_paid, slip.baseer_pending)

    def _baseer_accounting_moves(self):
        return self.move_id | self.baseer_correction_ids.filtered(lambda move: move.state == 'posted')

    def _baseer_payment_allocations(self):
        """Payment evidence, separately from reductions of the employee liability.

        For a payment with a write-off, only the liquidity-funded share counts as
        payment. Native posted payment state is sufficient on Community cash
        journals; is_matched also recognizes a completed bank matching workflow.
        """
        self.ensure_one()
        self.check_access('read')
        amounts = defaultdict(lambda: Decimal(0))
        payable = self._baseer_accounting_moves().line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable')
        for line in payable:
            for partial in line.matched_debit_ids | line.matched_credit_ids:
                other = partial.debit_move_id if partial.credit_move_id == line else partial.credit_move_id
                payment = other.move_id.origin_payment_id
                if (not payment or payment.state not in ('in_process', 'paid')
                        or payment.company_id != self.company_id or payment.move_id.state != 'posted'):
                    continue
                liquidity, counterpart, _writeoff = payment._seek_for_lines()
                cash = abs(sum((decimal(item.balance) for item in liquidity), Decimal(0)))
                debt = abs(sum((decimal(item.balance) for item in counterpart), Decimal(0)))
                share = min(Decimal(1), cash / debt) if debt else Decimal(0)
                direction = Decimal(1) if partial.credit_move_id == line else Decimal(-1)
                # Cumulative cent allocation conserves the payment even when a
                # write-off settles several salaries (e.g. 1 SAR cash / 3 debts).
                preceding = sum((decimal(item.amount) for item in
                    (counterpart.matched_debit_ids | counterpart.matched_credit_ids).sorted('id')
                    if item.id < partial.id), Decimal(0))
                funded = money((preceding + decimal(partial.amount)) * share) - money(preceding * share)
                amounts[payment] += funded * direction
        return [{'payment': payment, 'amount': money(amount),
                 'confirmed': payment.state == 'paid' or payment.is_matched}
                for payment, amount in amounts.items() if money(amount)]

    def _baseer_payments(self):
        self.check_access('read')
        return self._baseer_accounting_moves()._get_reconciled_payments().filtered(lambda p: p.state in ('in_process', 'paid'))

    def action_view_payments(self):
        self.ensure_one()
        return payments_action(self._baseer_payments())

    def action_open_finance(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Financial Record'),
                'res_model': 'hr.payslip', 'res_id': self.id, 'view_mode': 'form',
                'views': [(self.env.ref('baseer_payroll.view_baseer_financial_payslip_form').id, 'form')]}

    def action_open_run(self):
        self.ensure_one()
        return self.payslip_run_id._baseer_open_run()


class FinancialEmployee(models.Model):
    _inherit = 'hr.employee'

    baseer_financial_slip_ids = fields.One2many(
        'hr.payslip', compute='_compute_financial_record', string='Financial Record',
        groups='om_hr_payroll.group_hr_payroll_manager', compute_sudo=False)

    @api.depends('slip_ids', 'slip_ids.baseer_managed', 'slip_ids.state')
    def _compute_financial_record(self):
        for employee in self:
            employee.baseer_financial_slip_ids = employee.slip_ids.filtered(
                lambda s: s.baseer_managed and s.company_id == employee.company_id and s.state != 'cancel'
            ).sorted(lambda s: (s.date_from, s.id), reverse=True)


class FinancialRun(models.Model):
    _inherit = 'hr.payslip.run'

    baseer_payment_state = fields.Selection(STAGES, compute='_compute_payment_stage', string='Payment Status')
    baseer_payment_count = fields.Integer(compute='_compute_payment_count', string='Payment History')

    @api.depends('state', 'baseer_net', 'baseer_residual', 'baseer_paid', 'baseer_pending')
    def _compute_payment_stage(self):
        for run in self:
            run.baseer_payment_state = stage(run.state, run.baseer_net, run.baseer_residual, run.baseer_paid, run.baseer_pending)

    def _compute_payment_count(self):
        for run in self:
            run.baseer_payment_count = len(run.slip_ids._baseer_payments())

    def _baseer_open_run(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': self.name,
                'res_model': 'hr.payslip.run', 'res_id': self.id, 'view_mode': 'form',
                'views': [(self.env.ref('baseer_payroll.view_baseer_payroll_run_form').id, 'form')]}

    def action_pay(self):
        self.ensure_one()
        if not self.baseer_managed:
            return super().action_pay()
        require_manager(self)
        wizard = self.env['baseer.payroll.settlement'].create({'run_id': self.id})
        return {'type': 'ir.actions.act_window', 'name': _('Pay Salaries'),
                'res_model': wizard._name, 'res_id': wizard.id, 'view_mode': 'form', 'target': 'new',
                'views': [(self.env.ref('baseer_payroll.view_baseer_settlement_form').id, 'form')]}

    def action_view_payments(self):
        self.ensure_one()
        return payments_action(self.slip_ids._baseer_payments())

    def _baseer_payment_rows(self):
        self.ensure_one()
        self.check_access('read')
        rows = []
        labels = dict(self.env['account.payment']._fields['state']._description_selection(self.env))
        for slip in self.slip_ids.filtered(lambda s: s.state != 'cancel'):
            for allocation in slip._baseer_payment_allocations():
                payment, amount = allocation['payment'], allocation['amount']
                rows.append({'employee_name': slip.employee_id.name, 'date': fields.Date.to_string(payment.date),
                             'method': payment.journal_id.display_name, 'payment_name': payment.name,
                             'payment_id': payment.id, 'amount': float(money(amount)),
                             'state': labels[payment.state], 'confirmed': allocation['confirmed']})
        return sorted(rows, key=lambda row: (row['date'], row['payment_id'], row['employee_name']))

    def _baseer_payment_total(self):
        return float(money(sum((decimal(row['amount']) for row in self._baseer_payment_rows() if row['confirmed']), Decimal(0))))

    def action_print_payment_receipt(self):
        self.ensure_one()
        if not self._baseer_payment_rows():
            raise UserError(_('There are no recorded payments for this payroll.'))
        return self.env.ref('baseer_payroll.action_report_baseer_payment_receipt').report_action(self)
