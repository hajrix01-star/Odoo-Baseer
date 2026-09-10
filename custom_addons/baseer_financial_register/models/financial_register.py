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
    baseer_register_settled = fields.Monetary(string='Settled', compute='_compute_register_values', currency_field='currency_id')
    baseer_register_has_settlement = fields.Boolean(compute='_compute_register_values', search='_search_register_settlement')

    @api.depends('move_type', 'amount_total', 'amount_residual', 'amount_total_signed', 'amount_residual_signed', 'company_currency_id', 'origin_payment_id', 'statement_line_id', 'reversed_entry_id')
    def _compute_register_values(self):
        labels = {
            'out_invoice': _('Customer invoice'), 'out_refund': _('Customer credit note'),
            'out_receipt': _('Sales receipt'), 'in_invoice': _('Vendor bill'),
            'in_refund': _('Vendor credit note'), 'in_receipt': _('Purchase receipt'),
        }
        for move in self:
            value = decimal(move.amount_total) - decimal(move.amount_residual) if move.move_type in INVOICES else ZERO
            move.baseer_register_settled = float(rounded(value, move.currency_id))
            company_settled = decimal(move.amount_total_signed) - decimal(move.amount_residual_signed) if move.move_type in INVOICES else ZERO
            move.baseer_register_has_settlement = bool(rounded(company_settled, move.company_currency_id))
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
                else:
                    label = _('Journal entry')
                if move.reversed_entry_id:
                    label = _('Reversal: %s', label)
            move.baseer_register_source = label

    @api.model
    def _search_register_settlement(self, operator, value):
        if operator not in ('=', '!=', 'in', 'not in'):
            return NotImplemented
        values = value if operator in ('in', 'not in') else [value]
        if not isinstance(values, (list, tuple)) or any(type(v) is not bool for v in values):
            raise ValidationError(_('Choose a valid settlement filter.'))
        self.flush_model(['amount_total_signed', 'amount_residual_signed', 'company_id', 'move_type'])
        self.env['res.company'].flush_model(['currency_id'])
        self.env['res.currency'].flush_model(['rounding'])
        # This is a predicate, not a data-returning query: the outer ORM search still
        # applies the caller's complete account.move access and privacy rules.
        ids = SQL('''SELECT fl_move.id FROM account_move fl_move
            JOIN res_company fl_company ON fl_company.id = fl_move.company_id
            JOIN res_currency fl_currency ON fl_currency.id = fl_company.currency_id
            WHERE fl_move.move_type IN %s
              AND ROUND((fl_move.amount_total_signed::numeric - fl_move.amount_residual_signed::numeric)
                        / fl_currency.rounding::numeric) <> 0''', INVOICES)
        predicate = Domain('id', 'in', ids)
        accepted = set(values)
        if operator in ('!=', 'not in'):
            accepted = {True, False} - accepted
        if accepted == {True, False}:
            return Domain.TRUE
        if not accepted:
            return Domain.FALSE
        return predicate if True in accepted else ~predicate

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
        return {
            'type': 'ir.actions.act_window', 'name': _('Financial operations'),
            'res_model': 'account.move', 'view_mode': 'list,kanban,form',
            'mobile_view_mode': 'kanban',
            'views': [(self.env.ref('baseer_financial_register.view_register_list').id, 'list'),
                      (self.env.ref('baseer_financial_register.view_register_kanban').id, 'kanban'),
                      (self.env.ref('account.view_move_form').id, 'form')],
            'search_view_id': [self.env.ref('baseer_financial_register.view_register_search').id, 'Financial operations'],
            'domain': [('state', '=', 'posted')],
            'context': dict(self.env.context, create=False, edit=False, delete=False),
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

    @api.model
    def baseer_financial_register_kpis(self, domain=None):
        self._register_require_access()
        base_domain = self._register_domain(domain or [])
        invoice_domain = base_domain & Domain('move_type', 'in', INVOICES)
        query = self._search(invoice_domain)
        self.flush_model(['amount_total_signed', 'amount_residual_signed', 'company_id', 'move_type', 'payment_state'])
        self.env['res.company'].flush_model(['currency_id'])
        rows = self.env.execute_query(SQL('''
            SELECT fl_company.currency_id,
                   CASE WHEN account_move.move_type IN %s THEN 'customer' ELSE 'supplier' END,
                   COUNT(*), SUM(account_move.amount_total_signed::numeric),
                   SUM(account_move.amount_residual_signed::numeric),
                   COUNT(*) FILTER (WHERE account_move.payment_state = 'partial')
              FROM %s
              JOIN res_company fl_company ON fl_company.id = account_move.company_id
             WHERE %s
          GROUP BY 1, 2''', CUSTOMERS, query.from_clause, query.where_clause or SQL('TRUE')))
        totals = {(cid, scope): (count, decimal(total), decimal(residual), partial)
                  for cid, scope, count, total, residual, partial in rows}

        today = fields.Date.context_today(self)
        terms = [('display_type', '=', 'payment_term'), ('date_maturity', '<', today),
                 ('amount_residual', '!=', 0), ('account_id.account_type', 'in', ('asset_receivable', 'liability_payable'))]
        lines = self.env['account.move.line']
        line_query = lines._search(Domain(terms) & Domain('move_id', 'any', invoice_domain))
        lines.flush_model(['move_id', 'amount_residual', 'date_maturity', 'display_type', 'account_id'])
        overdue_rows = self.env.execute_query(SQL('''
            SELECT fl_company.currency_id,
                   CASE WHEN fl_move.move_type IN %s THEN 'customer' ELSE 'supplier' END,
                   SUM(account_move_line.amount_residual::numeric)
              FROM %s
              JOIN account_move fl_move ON fl_move.id = account_move_line.move_id
              JOIN res_company fl_company ON fl_company.id = fl_move.company_id
             WHERE %s
               AND fl_move.id IN (%s)
          GROUP BY 1, 2''', CUSTOMERS, line_query.from_clause, line_query.where_clause or SQL('TRUE'),
            query.select(SQL.identifier('account_move', 'id'))))
        overdue = {(cid, scope): decimal(amount) for cid, scope, amount in overdue_rows}
        # Show honest zero cards when the current search has no visible invoices.
        currency_ids = set(self.env.companies.currency_id.ids) | {row[0] for row in rows}
        groups = []
        for currency in self.env['res.currency'].browse(sorted(currency_ids)):
            sections = []
            for scope, label, types, sign in (
                ('customer', _('Customers'), CUSTOMERS, 1),
                ('supplier', _('Suppliers'), SUPPLIERS, -1),
            ):
                count, total, residual, partial = totals.get((currency.id, scope), (0, ZERO, ZERO, 0))
                scoped = Domain('move_type', 'in', types) & Domain('company_currency_id', '=', currency.id)
                values = [
                    ('total', _('Net invoices'), total * sign, Domain.TRUE, False,
                     _('Posted invoices and credit notes; journal entries and payments are excluded.')),
                    ('settled', _('Settled amount'), (total - residual) * sign,
                     Domain('baseer_register_has_settlement', '=', True), False,
                     _('Current settlements, including payments, credits and write-offs; not bank cash received.')),
                    ('outstanding', _('Outstanding'), residual * sign, Domain('amount_residual', '!=', 0), False,
                     _('Current outstanding invoice balance.')),
                    ('partial', _('Partially paid'), partial, Domain('payment_state', '=', 'partial'), True,
                     _('Number of documents with the native partially paid status.')),
                    ('overdue', _('Net overdue'), overdue.get((currency.id, scope), ZERO) * sign,
                     Domain('line_ids', 'any', Domain(terms)), False,
                     _('Open installments due before today; credit balances retain their sign.')),
                ]
                cards = [{'key': key, 'label': title, 'display': f'{value:,}' if integer else display(value, currency),
                          'is_count': integer, 'domain': list(scoped & drill), 'tooltip': tooltip}
                         for key, title, value, drill, integer, tooltip in values]
                sections.append({'key': scope, 'label': label, 'document_count_display': f'{count:,}', 'cards': cards})
            groups.append({'currency_id': currency.id, 'currency_name': currency.name, 'sections': sections})
        return {'currency_groups': groups, 'as_of': fields.Date.to_string(today)}
