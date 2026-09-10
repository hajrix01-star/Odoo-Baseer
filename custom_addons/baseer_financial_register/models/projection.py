"""One caller-visible native ledger projection for register rows and indicators."""
from decimal import InvalidOperation

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools import SQL

from .financial_register import CUSTOMERS, SUPPLIERS, INVOICES, ZERO, decimal, display


class RegisterProjection(models.Model):
    _inherit = 'account.move'

    @api.model
    def _register_projection_sql(self, domain=None):
        self._register_require_access()
        moves = self._search(self._register_domain(domain or []))
        lines = self.env['account.move.line']
        # Both queries enforce their own ACL/rules. The SQL intersection below
        # also binds every line to the exact visible move/search scope.
        line_query = lines._search(Domain('move_id.state', '=', 'posted'))
        self.flush_model(['state', 'company_id', 'move_type', 'amount_total_signed',
            'amount_residual_signed', 'payment_state', 'reversed_entry_id', 'reversed_pos_order_id'])
        lines.flush_model(['move_id', 'account_id', 'balance', 'amount_residual',
                           'date_maturity', 'display_type'])
        self.env['account.account'].flush_model(['account_type'])
        self.env['res.company'].flush_model(['currency_id'])
        self.env['res.currency'].flush_model(['rounding'])
        self.env['pos.session'].flush_model(['move_id', 'state', 'config_id'])
        self.env['pos.config'].flush_model(['company_id'])
        self.env['pos.order'].flush_model(['session_id', 'partner_id'])
        self.env['pos.payment'].flush_model(['session_id', 'payment_method_id', 'pos_order_id'])
        self.env['pos.payment.method'].flush_model(['receivable_account_id', 'split_transactions'])
        self.env['res.company'].flush_model(['account_default_pos_receivable_account_id'])
        self.env['res.partner'].flush_model(['commercial_partner_id', 'property_account_receivable_id'])
        # Native company-dependent property SQL includes its own default-value
        # semantics and field access check; do not reconstruct those defaults.
        partner_account = SQL('CASE %s ELSE NULL END', SQL(' ').join(
            SQL('WHEN cfg.company_id = %s THEN %s', company.id,
                self.env['res.partner'].with_company(company)._field_to_sql('accounting_partner', 'property_account_receivable_id'))
            for company in self.env.companies))
        return SQL('''WITH RECURSIVE pos_roots(id, company_id, session_id) AS (
              SELECT root.id, root.company_id, s.id
                FROM account_move root JOIN pos_session s ON s.move_id = root.id
                JOIN pos_config cfg ON cfg.id = s.config_id
               WHERE root.move_type = 'entry' AND root.company_id IN %s
                 AND cfg.company_id = root.company_id AND s.state = 'closed'
              UNION
              SELECT root.id, root.company_id, s.id
                FROM account_move root JOIN pos_order o ON o.id = root.reversed_pos_order_id
                JOIN pos_session s ON s.id = o.session_id JOIN pos_config cfg ON cfg.id = s.config_id
               WHERE root.move_type = 'entry' AND root.company_id IN %s
                 AND cfg.company_id = root.company_id AND s.state = 'closed'
            ), pos_family(id, company_id, session_id) AS (
              SELECT * FROM pos_roots
              UNION
              SELECT child.id, child.company_id, parent.session_id FROM account_move child
                JOIN pos_family parent ON child.reversed_entry_id = parent.id
                 AND child.company_id = parent.company_id
               WHERE child.move_type = 'entry'
            ), expected_accounts(session_id, account_id) AS (
              SELECT s.id, c.account_default_pos_receivable_account_id
                FROM pos_session s JOIN pos_config cfg ON cfg.id = s.config_id
                JOIN res_company c ON c.id = cfg.company_id
               WHERE s.id IN (SELECT session_id FROM pos_roots)
              UNION
              SELECT payment.session_id,
                     CASE WHEN method.split_transactions THEN %s
                          ELSE COALESCE(method.receivable_account_id, c.account_default_pos_receivable_account_id) END
                FROM pos_payment payment JOIN pos_payment_method method ON method.id = payment.payment_method_id
                JOIN pos_session s ON s.id = payment.session_id JOIN pos_config cfg ON cfg.id = s.config_id
                JOIN res_company c ON c.id = cfg.company_id
                JOIN pos_order o ON o.id = payment.pos_order_id
                LEFT JOIN res_partner customer ON customer.id = o.partner_id
                LEFT JOIN res_partner accounting_partner ON accounting_partner.id = customer.commercial_partner_id
               WHERE s.id IN (SELECT session_id FROM pos_roots)
            ), visible AS (
              SELECT account_move.id, account_move.company_id, account_move.move_type,
                     account_move.amount_total_signed::numeric AS native_total,
                     account_move.amount_residual_signed::numeric AS native_residual,
                     account_move.payment_state AS native_state,
                     c.currency_id, currency.rounding::numeric AS quantum,
                     family.id IS NOT NULL AS is_pos
                FROM %s
                JOIN res_company c ON c.id = account_move.company_id
                JOIN res_currency currency ON currency.id = c.currency_id
                LEFT JOIN (SELECT DISTINCT id FROM pos_family) family ON family.id = account_move.id
               WHERE %s
            ), line_totals AS (
              SELECT account_move_line.move_id,
                     COALESCE(SUM(account_move_line.balance::numeric)
                       FILTER (WHERE a.account_type = 'asset_receivable'), 0) AS pos_total,
                     COALESCE(SUM(account_move_line.amount_residual::numeric)
                       FILTER (WHERE a.account_type = 'asset_receivable'), 0) AS pos_residual,
                     COALESCE(SUM(account_move_line.amount_residual::numeric)
                       FILTER (WHERE a.account_type = 'asset_receivable'
                         AND account_move_line.date_maturity < %s), 0) AS pos_overdue,
                     COALESCE(SUM(account_move_line.amount_residual::numeric)
                       FILTER (WHERE a.account_type IN ('asset_receivable', 'liability_payable')
                         AND account_move_line.display_type = 'payment_term'
                         AND account_move_line.date_maturity < %s), 0) AS invoice_overdue,
                     COUNT(*) FILTER (WHERE a.account_type = 'asset_receivable') AS receivable_count,
                     COUNT(*) FILTER (WHERE account_move_line.balance <> 0) AS nonzero_count,
                     COUNT(*) FILTER (WHERE (account_move_line.display_type = 'payment_term'
                         OR EXISTS (SELECT 1 FROM expected_accounts expected JOIN pos_family f ON f.session_id = expected.session_id
                                     WHERE f.id = account_move_line.move_id AND expected.account_id = account_move_line.account_id))
                         AND account_move_line.balance <> 0
                         AND a.account_type <> 'asset_receivable') AS bad_pos_terms
                FROM %s
                JOIN account_account a ON a.id = account_move_line.account_id
               WHERE %s AND account_move_line.move_id IN (SELECT id FROM visible)
               GROUP BY account_move_line.move_id
            ), raw AS (
              SELECT v.*,
                     v.move_type IN %s AS is_invoice,
                     CASE WHEN v.move_type IN %s OR v.is_pos THEN 'customer'
                          WHEN v.move_type IN %s THEN 'supplier' END AS scope,
                     v.is_pos AND (COALESCE(l.bad_pos_terms,0) > 0 OR
                       (COALESCE(l.receivable_count,0) = 0 AND COALESCE(l.nonzero_count,0) > 0)) AS incomplete,
                     CASE WHEN v.move_type IN %s THEN -v.native_total
                          WHEN v.move_type IN %s THEN v.native_total
                          WHEN v.is_pos THEN COALESCE(l.pos_total,0) ELSE 0 END AS raw_total,
                     CASE WHEN v.move_type IN %s THEN -v.native_residual
                          WHEN v.move_type IN %s THEN v.native_residual
                          WHEN v.is_pos THEN COALESCE(l.pos_residual,0) ELSE 0 END AS raw_residual,
                     CASE WHEN v.move_type IN %s THEN -COALESCE(l.invoice_overdue,0)
                          WHEN v.move_type IN %s THEN COALESCE(l.invoice_overdue,0)
                          WHEN v.is_pos THEN COALESCE(l.pos_overdue,0) ELSE 0 END AS raw_overdue
                FROM visible v LEFT JOIN line_totals l ON l.move_id = v.id
            ), amounts AS (
              SELECT raw.*, ROUND(raw_total / quantum) * quantum AS total,
                     ROUND(raw_residual / quantum) * quantum AS outstanding,
                     ROUND(raw_overdue / quantum) * quantum AS overdue
                FROM raw
            ), flags AS (
              SELECT amounts.*, total - outstanding AS settled,
                     is_invoice OR (is_pos AND NOT incomplete
                       AND (total <> 0 OR outstanding <> 0 OR overdue <> 0)) AS contributor,
                     CASE WHEN is_invoice THEN native_state
                          WHEN is_pos AND NOT incomplete THEN
                            CASE WHEN outstanding = 0 THEN 'paid'
                                 WHEN total * outstanding > 0 AND ABS(outstanding) < ABS(total) THEN 'partial'
                                 ELSE 'not_paid' END END AS payment_state
                FROM amounts
            ) SELECT flags.*, contributor AND settled <> 0 AS has_settlement,
                     contributor AND overdue <> 0 AS is_overdue,
                     contributor AND payment_state = 'partial' AS is_partial
                FROM flags''', tuple(self.env.companies.ids), tuple(self.env.companies.ids), partner_account, moves.from_clause,
            moves.where_clause or SQL('TRUE'), fields.Date.context_today(self), fields.Date.context_today(self),
            line_query.from_clause, line_query.where_clause or SQL('TRUE'),
            INVOICES, CUSTOMERS, SUPPLIERS, SUPPLIERS, CUSTOMERS,
            SUPPLIERS, CUSTOMERS, SUPPLIERS, CUSTOMERS)

    @api.depends_context('uid', 'company', 'allowed_company_ids')
    @api.depends('state', 'move_type', 'amount_total_signed', 'amount_residual_signed', 'payment_state',
                 'line_ids.balance', 'line_ids.amount_residual', 'line_ids.date_maturity',
                 'line_ids.account_id.account_type', 'pos_session_ids.state', 'reversed_entry_id')
    def _compute_register_projection(self):
        self._register_require_access()
        projection = self._register_projection_sql([('id', 'in', self.ids)])
        rows = self.env.execute_query(SQL('''SELECT id, total, settled, outstanding, scope,
            contributor, incomplete, payment_state, has_settlement, is_overdue, is_pos FROM (%s) p''', projection))
        by_id = {row[0]: row[1:] for row in rows}
        for move in self:
            total, settled, outstanding, scope, contributes, incomplete, status, has_settlement, overdue, is_pos = by_id.get(
                move.id, (ZERO, ZERO, ZERO, None, False, False, None, False, False, False))
            move.baseer_register_amount = float(total if contributes or is_pos else decimal(move.amount_total_signed))
            move.baseer_register_settled = float(settled) if contributes else 0
            move.baseer_register_outstanding = float(outstanding) if contributes else 0
            move.baseer_register_scope = scope or False
            move.baseer_register_contributor = contributes
            move.baseer_register_incomplete = incomplete
            move.baseer_register_payment_state = status if contributes else False
            move.baseer_register_has_settlement = has_settlement
            move.baseer_register_is_overdue = overdue

    @api.model
    def _register_projection_search(self, key, operator, value, *, boolean=False, numeric=False):
        self._register_require_access()
        if key not in {'scope', 'contributor', 'payment_state', 'has_settlement', 'outstanding', 'is_overdue'}:
            raise ValidationError(_('Choose a valid financial operations filter.'))
        if operator not in ('=', '!=', 'in', 'not in', '<', '<=', '>', '>='):
            return NotImplemented
        values = value if operator in ('in', 'not in') else [value]
        if not isinstance(values, (list, tuple)):
            raise ValidationError(_('Choose a valid financial operations filter.'))
        if boolean:
            if any(type(item) is not bool for item in values) or operator not in ('=', '!=', 'in', 'not in'):
                raise ValidationError(_('Choose a valid financial operations filter.'))
        elif numeric:
            try:
                values = [decimal(item) for item in values]
                if any(not item.is_finite() for item in values):
                    raise InvalidOperation
            except (InvalidOperation, ValueError, TypeError):
                raise ValidationError(_('Choose a valid financial operations filter.')) from None
        elif any(item is not False and not isinstance(item, str) for item in values):
            raise ValidationError(_('Choose a valid financial operations filter.'))
        else:
            values = ['' if item is False else item for item in values]
        column = SQL.identifier('p', key)
        if not boolean and not numeric:
            column = SQL('COALESCE(%s, %s)', column, '')
        if operator in ('in', 'not in'):
            if not values:
                return Domain.FALSE if operator == 'in' else Domain.TRUE
            condition = SQL('%s IN %s', column, tuple(values))
            if operator == 'not in':
                condition = SQL('NOT (%s)', condition)
        else:
            operators = {'=': SQL('='), '!=': SQL('<>'), '<': SQL('<'), '<=': SQL('<='), '>': SQL('>'), '>=': SQL('>=')}
            condition = SQL('%s %s %s', column, operators[operator], values[0])
        return Domain('id', 'in', SQL('SELECT p.id FROM (%s) p WHERE %s',
            self._register_projection_sql(), condition))

    @api.model
    def baseer_financial_register_kpis(self, domain=None):
        self._register_require_access()
        projection = self._register_projection_sql(domain or [])
        rows = self.env.execute_query(SQL('''SELECT currency_id, scope,
                COUNT(*) FILTER (WHERE contributor),
                COALESCE(SUM(total) FILTER (WHERE contributor),0),
                COALESCE(SUM(settled) FILTER (WHERE contributor),0),
                COALESCE(SUM(outstanding) FILTER (WHERE contributor),0),
                COUNT(*) FILTER (WHERE is_partial),
                COALESCE(SUM(overdue) FILTER (WHERE contributor),0),
                COUNT(*) FILTER (WHERE incomplete)
            FROM (%s) p GROUP BY currency_id, scope''', projection))
        totals = {(row[0], row[1]): row[2:] for row in rows}
        currency_ids = set(self.env.companies.currency_id.ids) | {row[0] for row in rows}
        uncovered = sum(row[-1] for row in rows)
        groups = []
        for currency in self.env['res.currency'].browse(sorted(currency_ids)):
            sections = []
            for scope, label in [('customer', _('Sales')), ('supplier', _('Suppliers'))]:
                count, total, settled, outstanding, partial, overdue, _unknown = totals.get(
                    (currency.id, scope), (0, ZERO, ZERO, ZERO, 0, ZERO, 0))
                scoped = Domain([('baseer_register_scope', '=', scope),
                    ('baseer_register_contributor', '=', True), ('company_currency_id', '=', currency.id)])
                values = [
                    ('total', _('Net sales') if scope == 'customer' else _('Net invoices'), total, Domain.TRUE, False,
                     _('Posted invoices and native POS sales, net of credit notes and sales reversals.') if scope == 'customer'
                     else _('Posted vendor bills and credit notes; journal entries and payments are excluded.')),
                    ('settled', _('Settled amount'), settled, Domain('baseer_register_has_settlement', '=', True), False,
                     _('Current customer or supplier settlements, including credits and write-offs; not bank cash received.')),
                    ('outstanding', _('Outstanding'), outstanding, Domain('baseer_register_outstanding', '!=', 0), False,
                     _('Current outstanding receivable or payable balance.')),
                    ('partial', _('Partially settled operations'), partial, Domain('baseer_register_payment_state', '=', 'partial'), True,
                     _('Partially settled invoices or POS accounting operations; a session may contain several orders.')),
                    ('overdue', _('Net overdue'), overdue, Domain('baseer_register_is_overdue', '=', True), False,
                     _('Open balances with a recorded due date before today; credits retain their sign.')),
                ]
                cards = [{'key': key, 'label': title, 'display': f'{value:,}' if integer else display(value, currency),
                    'is_count': integer, 'domain': list(scoped & drill), 'tooltip': tooltip}
                    for key, title, value, drill, integer, tooltip in values]
                sections.append({'key': scope, 'label': label, 'document_count_display': f'{count:,}', 'cards': cards})
            groups.append({'currency_id': currency.id, 'currency_name': currency.name, 'sections': sections})
        return {'currency_groups': groups, 'as_of': fields.Date.to_string(fields.Date.context_today(self)),
            'uncovered_count': uncovered, 'coverage_warning':
                _('Some POS entries have incomplete receivable account classification and are excluded from the totals.') if uncovered else ''}
