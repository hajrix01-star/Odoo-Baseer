"""Read native posted evidence; never post, reconcile or duplicate financial data."""
from decimal import Decimal, ROUND_HALF_UP

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Domain
from odoo.tools import SQL


CUSTOMERS = ('out_invoice', 'out_refund', 'out_receipt')
SUPPLIERS = ('in_invoice', 'in_refund', 'in_receipt')
INVOICES = CUSTOMERS + SUPPLIERS
ZERO = Decimal('0')
ACCOUNTING_GROUPS = 'account.group_account_invoice,account.group_account_readonly'


def decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def rounded(value, currency):
    quantum = decimal(currency.rounding)
    return (decimal(value) / quantum).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * quantum


def display(value, currency):
    amount = rounded(value, currency)
    return f'{amount if amount else abs(amount):,.{currency.decimal_places}f}'


class FinancialRegister(models.Model):
    _inherit = 'account.move'

    baseer_register_source = fields.Char(string='Operation', compute='_compute_register_values')
    baseer_register_amount = fields.Monetary(string='Amount', compute='_compute_register_projection',
        currency_field='company_currency_id', groups=ACCOUNTING_GROUPS)
    baseer_register_settled = fields.Monetary(string='Settled', compute='_compute_register_projection',
        currency_field='company_currency_id', groups=ACCOUNTING_GROUPS)
    baseer_register_outstanding = fields.Monetary(string='Outstanding', compute='_compute_register_projection',
        search='_search_register_outstanding', currency_field='company_currency_id', groups=ACCOUNTING_GROUPS)
    baseer_register_has_settlement = fields.Boolean(compute='_compute_register_projection',
        search='_search_register_settlement', groups=ACCOUNTING_GROUPS)
    baseer_register_scope = fields.Selection([('customer', 'Sales'), ('supplier', 'Suppliers')],
        compute='_compute_register_projection', search='_search_register_scope', groups=ACCOUNTING_GROUPS)
    baseer_register_contributor = fields.Boolean(compute='_compute_register_projection',
        search='_search_register_contributor', groups=ACCOUNTING_GROUPS)
    baseer_register_incomplete = fields.Boolean(compute='_compute_register_projection', groups=ACCOUNTING_GROUPS)
    baseer_register_is_overdue = fields.Boolean(compute='_compute_register_projection',
        search='_search_register_overdue', groups=ACCOUNTING_GROUPS)
    baseer_register_payment_state = fields.Selection([
        ('not_paid', 'Not Paid'), ('in_payment', 'In Payment'), ('paid', 'Paid'),
        ('partial', 'Partially Paid'), ('reversed', 'Reversed'), ('blocked', 'Blocked'),
        ('invoicing_legacy', 'Invoicing App Legacy')], string='Payment Status',
        compute='_compute_register_projection', search='_search_register_payment_state', groups=ACCOUNTING_GROUPS)

    @api.depends('move_type', 'amount_total', 'amount_residual', 'amount_total_signed', 'amount_residual_signed', 'company_currency_id', 'origin_payment_id', 'statement_line_id', 'reversed_entry_id')
    def _compute_register_values(self):
        labels = {
            'out_invoice': _('Customer invoice'), 'out_refund': _('Customer credit note'),
            'out_receipt': _('Sales receipt'), 'in_invoice': _('Vendor bill'),
            'in_refund': _('Vendor credit note'), 'in_receipt': _('Purchase receipt'),
        }
        for move in self:
            label = labels.get(move.move_type)
            if not label:
                if 'baseer_pos_summary_id' in move._fields and move.baseer_pos_summary_id:
                    label = _('Sales summary')
                elif 'baseer_loan_id' in move._fields and move.baseer_loan_id:
                    label = _('Employee advance')
                elif 'baseer_payslip_id' in move._fields and move.baseer_payslip_id:
                    label = _('Payroll')
                elif move.origin_payment_id:
                    label = _('Incoming payment') if move.origin_payment_id.payment_type == 'inbound' else _('Outgoing payment')
                elif move.statement_line_id:
                    label = _('Bank / cash movement')
                elif move.pos_session_ids:
                    label = _('POS session sales')
                elif move.reversed_pos_order_id:
                    label = _('POS invoice reclassification')
                else:
                    label = _('Journal entry')
                if move.reversed_entry_id:
                    label = _('Reversal: %s', label)
            move.baseer_register_source = label

    @api.model
    def _search_register_settlement(self, operator, value):
        return self._register_projection_search('has_settlement', operator, value, boolean=True)

    def _search_register_outstanding(self, operator, value):
        return self._register_projection_search('outstanding', operator, value, numeric=True)

    def _search_register_scope(self, operator, value):
        return self._register_projection_search('scope', operator, value)

    def _search_register_contributor(self, operator, value):
        return self._register_projection_search('contributor', operator, value, boolean=True)

    def _search_register_overdue(self, operator, value):
        return self._register_projection_search('is_overdue', operator, value, boolean=True)

    def _search_register_payment_state(self, operator, value):
        return self._register_projection_search('payment_state', operator, value)

    @api.model
    def _register_require_access(self):
        if self.env.user.has_group('baseer_access_roles.group_cashier') or not (
            self.env.user.has_group('account.group_account_invoice')
            or self.env.user.has_group('account.group_account_readonly')
        ):
            raise AccessError(_('Financial operations are available to authorized accounting users only.'))
        self.check_access('read')

    @api.model
    def action_open_financial_register(self):
        self._register_require_access()
        context = dict(self.env.context, create=False, edit=False, delete=False)
        context.pop('baseer_register_cash_month', None)
        return {
            'type': 'ir.actions.act_window', 'name': _('Financial operations'),
            'res_model': 'account.move', 'view_mode': 'list,kanban,form',
            'mobile_view_mode': 'kanban',
            'views': [(self.env.ref('baseer_financial_register.view_register_list').id, 'list'),
                      (self.env.ref('baseer_financial_register.view_register_kanban').id, 'kanban'),
                      (self.env.ref('account.view_move_form').id, 'form')],
            'search_view_id': [self.env.ref('baseer_financial_register.view_register_search').id, 'Financial operations'],
            'domain': [('state', '=', 'posted')],
            'context': context,
            'target': 'current',
        }

    def action_open_register_source(self):
        self._register_require_access()
        self.ensure_one()
        self.check_access('read')
        for field_name in ('baseer_pos_summary_id', 'baseer_loan_id', 'baseer_payslip_id',
                           'baseer_eos_id', 'baseer_hr_service_id', 'origin_payment_id', 'statement_line_id'):
            if field_name in self._fields and self[field_name]:
                source = self[field_name]
                source.check_access('read')
                return source.get_formview_action()
        return self.get_formview_action()

    @api.model
    def _register_domain(self, domain):
        # Public search filters cannot request the private any! rule operator.
        def check(nodes):
            if not isinstance(nodes, (list, tuple)):
                raise ValidationError(_('Choose a valid financial operations filter.'))
            for leaf in nodes:
                if isinstance(leaf, (tuple, list)) and len(leaf) == 3:
                    operator = leaf[1].lower() if isinstance(leaf[1], str) else leaf[1]
                    if operator in ('any!', 'not any!'):
                        raise AccessError(_('This search operator is reserved for access rules.'))
                    if operator in ('any', 'not any'):
                        check(leaf[2])
        check(domain)
        return Domain('state', '=', 'posted') & Domain(domain)

