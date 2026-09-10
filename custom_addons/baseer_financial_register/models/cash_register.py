"""Cash preview: consume the existing report's final fragments, never retrace them."""
from calendar import monthrange
from datetime import date
from decimal import Decimal
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from .financial_register import ZERO, display


class CashReportCapture(models.AbstractModel):
    _inherit = 'eh.account.dynamic.report.handler.baseer_cash_categories'

    def _payload(self, movements, opening, change, state, options, company, date_from, date_to,
                 with_sources=False, internal=None):
        result = super()._payload(movements, opening, change, state, options, company, date_from,
                                  date_to, with_sources=with_sources, internal=internal)
        if internal is not None and internal.get('baseer_register_capture'):
            # Fragments have already passed the reference report's transfer,
            # allocation and reconciliation checks. Only regroup them for rows.
            cash_lines = self.env['account.move.line'].browse(sorted({m['cash_source'] for m in movements}))
            cash_lines.check_access('read')
            moves = cash_lines.move_id
            moves.check_access('read')
            line_moves = {line.id: line.move_id.id for line in cash_lines}
            rows = {}
            for movement in movements:
                row = rows.setdefault(line_moves[movement['cash_source']], {'receipts': ZERO, 'payments': ZERO})
                key = 'receipts' if movement['direction'] == 'in' else 'payments'
                row[key] += movement['gross']
            for row in rows.values():
                row['net'] = row['receipts'] + row['payments']
            internal['baseer_register_rows'] = rows
        return result


class CashFinancialRegister(models.Model):
    _inherit = 'account.move'

    baseer_register_cash_receipts = fields.Monetary(string='Actual receipts', currency_field='company_currency_id',
        compute='_compute_register_cash', search='_search_register_cash_receipts')
    baseer_register_cash_payments = fields.Monetary(string='Actual payments', currency_field='company_currency_id',
        compute='_compute_register_cash', search='_search_register_cash_payments')
    baseer_register_cash_net = fields.Monetary(string='Net cash movement', currency_field='company_currency_id',
        compute='_compute_register_cash')
    baseer_register_cash_visible = fields.Boolean(compute='_compute_register_cash', search='_search_register_cash_visible')

    @api.model
    def _register_cash_month(self, month=None):
        value = month if month is not None else self.env.context.get('baseer_register_cash_month')
        if value is None:
            value = fields.Date.context_today(self).strftime('%Y-%m')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
            raise UserError(_('Choose a valid calendar month.'))
        try:
            year, number = map(int, value.split('-'))
            start = date(year, number, 1)
            end = date(year, number, monthrange(year, number)[1])
        except ValueError:
            raise UserError(_('Choose a valid calendar month.')) from None
        return value, start, end

    @api.model
    def _register_cash_snapshot(self, month=None):
        self._register_require_access()
        value, start, end = self._register_cash_month(month)
        company = self.env.company
        company.check_access('read')
        options = {'company_ids': [company.id], 'posted_only': True, 'baseer_include_tax': True,
                   'date': {'mode': 'range', 'date_from': start.isoformat(), 'date_to': end.isoformat()}}
        handler = self.env['eh.account.dynamic.report.handler.baseer_cash_categories']
        handler = handler._authorized_report_handler(options)
        capture = {'baseer_register_capture': True}
        payload = handler._compute_report(options, internal=capture)
        rows = capture['baseer_register_rows']
        totals = payload['meta']['exact_totals']
        for key, report_key in [('receipts', 'receipts'), ('payments', 'payments'), ('net', 'actual_net_movement')]:
            if sum((row[key] for row in rows.values()), ZERO) != Decimal(totals[report_key]):
                raise UserError(_('Cash rows do not match the cash movement report.'))
        return value, company, payload, rows

    @api.depends_context('baseer_register_cash_month', 'company', 'uid')
    def _compute_register_cash(self):
        _month, _company, _report, rows = self._register_cash_snapshot()
        for move in self:
            row = rows.get(move.id, {})
            move.baseer_register_cash_receipts = float(row.get('receipts', ZERO))
            move.baseer_register_cash_payments = float(row.get('payments', ZERO))
            move.baseer_register_cash_net = float(row.get('net', ZERO))
            move.baseer_register_cash_visible = move.id in rows

    @api.model
    def _cash_nonzero_search(self, key, operator, value):
        self._register_require_access()
        values = value if operator in ('in', 'not in') else [value]
        if operator not in ('=', '!=', 'in', 'not in') or values not in ([0], (0,)):
            return NotImplemented
        _month, _company, _report, rows = self._register_cash_snapshot()
        predicate = Domain('id', 'in', [ident for ident, row in rows.items() if row[key]])
        return predicate if operator in ('!=', 'not in') else ~predicate

    @api.model
    def _search_register_cash_receipts(self, operator, value):
        return self._cash_nonzero_search('receipts', operator, value)

    @api.model
    def _search_register_cash_payments(self, operator, value):
        return self._cash_nonzero_search('payments', operator, value)

    @api.model
    def _search_register_cash_visible(self, operator, value):
        self._register_require_access()
        if operator not in ('=', '!=', 'in', 'not in'):
            return NotImplemented
        values = value if operator in ('in', 'not in') else [value]
        if not isinstance(values, (list, tuple)) or any(type(item) is not bool for item in values):
            raise UserError(_('Choose a valid cash movement filter.'))
        _month, _company, _report, rows = self._register_cash_snapshot()
        predicate = Domain('id', 'in', sorted(rows))
        accepted = set(values)
        if operator in ('!=', 'not in'):
            accepted = {True, False} - accepted
        if accepted == {True, False}:
            return Domain.TRUE
        if not accepted:
            return Domain.FALSE
        return predicate if True in accepted else ~predicate

    @api.model
    def action_open_register_cash(self, month=None):
        self._register_require_access()
        value, _start, _end = self._register_cash_month(month)
        # A fresh action clears obsolete invoice facets; subsequent native facets
        # narrow both the monetary rows and their cards through the same query.
        context = dict(self.env.context, baseer_register_cash_month=value,
                       allowed_company_ids=[self.env.company.id], create=False, edit=False, delete=False)
        return {'type': 'ir.actions.act_window', 'name': _('Cash movements'), 'res_model': 'account.move',
                'view_mode': 'list,kanban,form', 'mobile_view_mode': 'kanban', 'target': 'current',
                'views': [(self.env.ref('baseer_financial_register.view_cash_register_list').id, 'list'),
                          (self.env.ref('baseer_financial_register.view_cash_register_kanban').id, 'kanban'),
                          (self.env.ref('account.view_move_form').id, 'form')],
                'search_view_id': [self.env.ref('baseer_financial_register.view_cash_register_search').id, _('Cash movements')],
                'domain': [('state', '=', 'posted'), ('baseer_register_cash_visible', '=', True)],
                'context': context}

    @api.model
    def baseer_financial_register_cash_kpis(self, domain=None):
        month, company, report, rows = self._register_cash_snapshot()
        # Untrusted search facets never supply a monetary value. Native record
        # rules intersect the bounded report sources, including move-level rules.
        visible = self.search(self._register_domain(domain or []) & Domain('id', 'in', sorted(rows)))
        sums = {key: sum((rows[move.id][key] for move in visible), ZERO) for key in ('receipts', 'payments', 'net')}
        cards = []
        for key, title, predicate in (
            ('receipts', _('Actual receipts'), [('baseer_register_cash_receipts', '!=', 0)]),
            ('payments', _('Actual payments'), [('baseer_register_cash_payments', '!=', 0)]),
            ('net', _('Net cash movement'), []),
        ):
            cards.append({'key': key, 'label': title, 'display': display(sums[key], company.currency_id),
                          'is_count': False, 'domain': predicate,
                          'tooltip': _('Same cash allocation as the cash movement report, including tax; current search filters apply.')})
        return {'currency_groups': [{'currency_id': company.currency_id.id, 'currency_name': company.currency_id.name,
                    'sections': [{'key': 'cash', 'label': _('Cash movements'), 'document_count_display': f'{len(visible):,}', 'cards': cards}]}],
                'cash_month': month, 'company_name': company.name,
                'as_of': report['meta']['date_to']}
