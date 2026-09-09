"""Native departure to existing award, payment and scoped signature receipt."""
from decimal import Decimal
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import clean_context
from .common import INTERNAL, money, decimal, require_manager, lock_employee
from .end_service import internal


STAGES = [('draft', 'Review'), ('awaiting', 'Awaiting Payment'), ('partial', 'Partially Paid'),
          ('processing', 'Payment under review'), ('paid', 'Paid'), ('reversed', 'Cancelled or Reversed')]


def short(value, limit=140):
    value = ' '.join(str(value or '').split())
    return value if len(value) <= limit else value[:limit - 1] + '…'


class Employee(models.Model):
    _inherit = 'hr.employee'

    baseer_has_departure = fields.Boolean(compute='_compute_baseer_has_departure',
        groups='om_hr_payroll.group_hr_payroll_manager', compute_sudo=False)

    @api.depends('version_id', 'version_id.departure_date')
    def _compute_baseer_has_departure(self):
        can_read = self.env.user.has_group('hr.group_hr_user')
        for employee in self:
            employee.baseer_has_departure = bool(can_read and employee.version_id.departure_date)

    def action_open_departure_award(self):
        self.ensure_one()
        require_manager(self)
        if not self.env.user.has_group('hr.group_hr_user'):
            raise AccessError(_('HR access is required to read the employee departure.'))
        lock_employee(self.env, self.id, self.company_id.id)
        version = self.version_id
        version.invalidate_recordset()
        if not version.departure_date or not version.departure_reason_id:
            raise UserError(_('Record the employee departure date and reason first.'))
        awards = self.env['baseer.hr.eos'].search([('employee_id', '=', self.id),
            ('company_id', '=', self.company_id.id), ('service_end', '=', version.departure_date)], order='id desc')
        award = awards.filtered(lambda r: r.state == 'approved')[:1] or awards[:1]
        if not award:
            reason = 'review'
            for xmlid, code in [('hr.departure_resigned', 'resignation'), ('hr.departure_fired', 'termination'), ('hr.departure_retired', 'termination')]:
                if version.departure_reason_id == self.env.ref(xmlid, raise_if_not_found=False):
                    reason = code
                    break
            award = self.env['baseer.hr.eos'].with_context(dict(clean_context(self.env.context), baseer_payroll_internal=INTERNAL)).create({
                'employee_id': self.id, 'company_id': self.company_id.id, 'version_id': version.id,
                'departure_entry': True,
                'service_end': version.departure_date, 'reason': reason,
                'evidence_reference': _('Employee departure / %s / %s', self.id, version.departure_date),
            })
            award.action_calculate()
        return award._open_workflow()


class Award(models.Model):
    _inherit = 'baseer.hr.eos'

    document_snapshot = fields.Json(readonly=True, copy=False)
    departure_entry = fields.Boolean(readonly=True, copy=False)
    _departure_entry_unique = models.UniqueIndex('(employee_id, service_end) WHERE departure_entry',
        'This departure is already being processed. Reopen End of Service and Clearance from the employee.')
    workflow_stage = fields.Selection(STAGES, compute='_compute_workflow_stage', string='Progress')
    received_amount = fields.Monetary(compute='_compute_workflow_stage', string='Confirmed paid amount')

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self) and (self.env.context.get('default_document_snapshot') or self.env.context.get('default_departure_entry') or any(v.get('document_snapshot') or v.get('departure_entry') for v in vals_list)):
            raise AccessError(_('Document identity is captured only during award approval.'))
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self) and set(vals) & {'document_snapshot', 'departure_entry'}:
            raise AccessError(_('Document identity is captured only during award approval.'))
        return super().write(vals)

    def _payment_evidence(self):
        self.ensure_one()
        self.check_access('read')
        bill = self.bill_id
        if self.state != 'approved' or not bill:
            return 'draft', money(0), []
        if bill.state != 'posted' or bill.reversal_move_ids.filtered(lambda m: m.state == 'posted'):
            return 'reversed', money(0), []
        amounts = {}
        for line in bill.line_ids.filtered(lambda l: l.account_id.account_type == 'liability_payable'):
            for partial in line.matched_debit_ids:
                payment = partial.debit_move_id.move_id.origin_payment_id
                if payment and payment.state == 'paid' and payment.is_matched and payment.payment_type == 'outbound' and payment.company_id == self.company_id:
                    # A native payment may settle debt partly through a write-off.
                    # Such mixed settlements require review, not a full cash receipt.
                    if any(money(line.balance) for line in payment._seek_for_lines()[2]):
                        continue
                    amounts[payment] = amounts.get(payment, Decimal(0)) + decimal(partial.amount)
        paid = money(sum(amounts.values(), Decimal(0)))
        rows = [{'date': fields.Date.to_string(p.date), 'method': p.journal_id.name,
                 'reference': p.name, 'amount': money(amount)} for p, amount in amounts.items()]
        rows.sort(key=lambda r: (r['date'], r['reference'] or ''))
        if bill.payment_state == 'paid' and money(bill.amount_residual) == 0 and paid == money(self.award_amount):
            stage = 'paid'
        elif paid > 0:
            stage = 'partial'
        elif bill.payment_state == 'in_payment' or money(bill.amount_residual) < money(self.award_amount):
            stage = 'processing'
        else:
            stage = 'awaiting'
        return stage, paid, rows

    @api.depends('state', 'bill_id.state', 'bill_id.payment_state', 'bill_id.amount_residual',
                 'bill_id.reversal_move_ids.state', 'bill_id.line_ids.matched_debit_ids',
                 'bill_id.line_ids.matched_debit_ids.debit_move_id.move_id.origin_payment_id.state',
                 'bill_id.line_ids.matched_debit_ids.debit_move_id.move_id.origin_payment_id.is_matched')
    def _compute_workflow_stage(self):
        for award in self:
            stage, paid, rows = award._payment_evidence()
            award.workflow_stage = stage
            award.received_amount = float(paid)

    def _open_workflow(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('End of Service and Clearance'),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
            'views': [(self.env.ref('baseer_payroll.view_baseer_eos_form').id, 'form')]}

    def _document_identity(self):
        self.ensure_one()
        employee, company = self.employee_id, self.company_id
        return {'company_name': company.name, 'company_address': ', '.join(filter(None, [company.street, company.city, company.country_id.name])),
            'company_vat': company.vat, 'employee_name': employee.name,
            'employee_identifier': employee.identification_id or str(employee.id), 'job_title': employee.job_title,
            'departure_reason': self.version_id.departure_reason_id.name,
            'departure_reason_ar': self.version_id.departure_reason_id.with_context(lang='ar_001').name,
            'departure_reason_en': self.version_id.departure_reason_id.with_context(lang='en_US').name,
            'approved_by': self.approved_by_id.name or self.env.user.name}

    def action_approve(self):
        was_approved = self.state == 'approved'
        result = super().action_approve()
        if not was_approved and not self.document_snapshot:
            self.with_context(baseer_payroll_internal=INTERNAL).write({'document_snapshot': self._document_identity()})
        return result

    def action_approve_workflow(self):
        self.ensure_one()
        self.action_approve()
        return self._open_workflow()

    def action_pay_award(self):
        self.ensure_one()
        require_manager(self)
        if not self.env.user.has_group('account.group_account_user'):
            raise AccessError(_('Accounting access is required to pay the award bill.'))
        if self.state != 'approved' or not self.bill_id or self.bill_id.state != 'posted' or self.bill_id.reversal_move_ids.filtered(lambda m: m.state == 'posted'):
            raise UserError(_('Issue a valid award bill before recording payment.'))
        if money(self.bill_id.amount_residual) <= 0:
            raise UserError(_('There is no remaining bill amount to pay.'))
        return self.bill_id.with_context(clean_context(self.env.context)).action_register_payment()

    def action_print_clearance(self):
        self.ensure_one()
        self._report_row()
        return self.env.ref('baseer_payroll.action_report_baseer_end_service').report_action(self)

    def _report_row(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group('om_hr_payroll.group_hr_payroll_manager'):
            raise AccessError(_('Payroll manager access is required.'))
        if not self.source_snapshot:
            raise UserError(_('Calculate and review the award before printing.'))
        if self.state == 'draft' and self.source_snapshot != self._source():
            raise UserError(_('Salary or service information changed. Recalculate the estimate and review it before approval.'))
        stage, paid, payments = self._payment_evidence()
        identity = self.document_snapshot or self._document_identity()
        final = stage == 'paid'
        fmt = lambda value: format(money(value), ',.2f')
        row = {key: short(value) for key, value in identity.items()}
        reason_key = 'departure_reason_ar' if (self.env.context.get('lang') or self.env.user.lang or '').startswith('ar') else 'departure_reason_en'
        row['departure_reason'] = short(identity.get(reason_key) or identity.get('departure_reason'))
        if self.state != 'approved':
            row['approved_by'] = ''
        row.update({'id': self.id, 'reference': f'EOS/{self.id:05d}',
            'title': _('Final End-of-Service Receipt') if final else _('End-of-Service Statement'),
            'is_final': final, 'stage_label': dict(self._fields['workflow_stage']._description_selection(self.env))[stage],
            'company_logo': False if self.company_id.uses_default_logo else self.company_id.logo,
            'service_start': fields.Date.to_string(self.service_start),
            'service_end': fields.Date.to_string(self.service_end), 'service_days': self.service_days,
            'calculation_policy': _('Gregorian anniversaries; final day included') if self.source_snapshot.get('policy', '').startswith('SA-EOS-V2') and self.source_snapshot.get('end_inclusive') else
                _('Gregorian anniversaries; final day excluded') if self.source_snapshot.get('policy', '').startswith('SA-EOS-V2') else _('Historical actual-days/365 policy; final day excluded'),
            'wage': fmt(self.eos_wage), 'full_award': fmt(self.full_award),
            'factor': fmt(decimal(self.entitlement_factor) * 100) + '%', 'award': fmt(self.award_amount), 'paid': fmt(paid),
            'remaining': fmt(max(decimal(self.award_amount) - paid, Decimal(0))),
            'currency': self.currency_id.name, 'bill_reference': short(self.bill_id.name or '—', 60),
            'payment_methods': short(' / '.join(dict.fromkeys(p['method'] for p in payments)), 100) or '—',
            'payment_date': payments[-1]['date'] if payments else '—',
            'approval_reference': short(self.evidence_reference, 100),
            'approval_date': fields.Date.to_string(self.approved_at.date()) if self.approved_at else '—',
            'note': short(self.evidence_note, 120),
            'acknowledgment': _('I acknowledge receipt of the end-of-service award amount stated above. This acknowledgment covers only the listed amount and does not waive any rights or amounts not stated in this document.') if final else
                _('This document states the calculated end-of-service award and recorded payments. It is not an acknowledgment of full receipt. Any remaining amount must be settled before signing the final receipt.'),
        })
        return row


class AwardReport(models.AbstractModel):
    _name = 'report.baseer_payroll.report_end_service'
    _description = 'End of Service Signature Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        if not docids:
            raise UserError(_('Choose an end-of-service award to print.'))
        docs = self.env['baseer.hr.eos'].browse(docids)
        docs.check_access('read')
        rows = [doc._report_row() for doc in docs]
        lang = self.env.context.get('lang') or self.env.user.lang or 'en_US'
        return {'doc_ids': docs.ids, 'doc_model': docs._name, 'docs': docs,
                'report_rows': rows, 'lang': lang, 'arabic': lang.startswith('ar')}
