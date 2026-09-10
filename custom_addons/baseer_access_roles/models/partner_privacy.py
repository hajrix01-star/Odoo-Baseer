"""Partner operational balances must obey the same ledger visibility as their caller."""
from odoo import api, models
from odoo.tools import SQL

from .payroll_privacy import limited


_PARTNER_BALANCE_QUERY = object()
_QUERY_CONTEXT_KEY = 'baseer_partner_balance_query'


class PartnerBalanceLineQuery(models.Model):
    _inherit = 'account.move.line'

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None, *, active_test=True,
                bypass_access=False):
        target = self
        if (self.env.context.get(_QUERY_CONTEXT_KEY) is _PARTNER_BALANCE_QUERY
                and limited(self.env)):
            # Consume the private capability on this one top-level native balance query.
            # Nested any! rule expressions must retain their native semantics: stripping
            # their bypass would both recurse and hide the private lines being detected.
            context = dict(self.env.context)
            context.pop(_QUERY_CONTEXT_KEY)
            target = self.with_context(context).sudo(False)
            bypass_access = False
        return super(PartnerBalanceLineQuery, target)._search(
            domain, offset=offset, limit=limit, order=order,
            active_test=active_test, bypass_access=bypass_access,
        )


class PartnerPrivacy(models.Model):
    _inherit = 'res.partner'

    @api.depends_context('company', 'uid')
    def _credit_debit_get(self):
        if not limited(self.env):
            return super()._credit_debit_get()
        # Reuse Odoo's full native calculation unchanged: same residual SUM, account
        # types, sign, unreconciled filter and root-company scope. Only its deliberate
        # access-bypassing source query is replaced by the caller-visible query above.
        target = self.with_context(**{_QUERY_CONTEXT_KEY: _PARTNER_BALANCE_QUERY})
        return super(PartnerPrivacy, target)._credit_debit_get()

    def _asset_difference_search(self, account_type, operator, operand):
        if not limited(self.env):
            return super()._asset_difference_search(account_type, operator, operand)
        if operator not in ('<', '=', '>', '>=', '<=') or not isinstance(operand, (float, int)):
            return []
        # Native search uses raw SQL and exposes no query hook. Preserve its active
        # account / posted / root-company grouping and residual sign, but intersect
        # its ledger source with the caller's ACL and global record rules.
        lines = self.env['account.move.line'].sudo(False)
        query = lines._search([
            ('parent_state', '=', 'posted'),
            ('company_id', 'child_of', self.env.company.root_id.id),
        ])
        lines.flush_model(['account_id', 'amount_residual', 'company_id',
                           'parent_state', 'partner_id'])
        self.env['account.account'].flush_model(['account_type', 'active'])
        sign = -1 if account_type == 'liability_payable' else 1
        sql = SQL("""
            SELECT account_move_line.partner_id
              FROM %s
              JOIN account_account balance_account ON balance_account.id = account_move_line.account_id
             WHERE balance_account.account_type = %s
               AND balance_account.active
               AND %s
          GROUP BY account_move_line.partner_id
            HAVING %s * COALESCE(SUM(account_move_line.amount_residual), 0) %s %s
        """, query.from_clause, account_type, query.where_clause or SQL('TRUE'),
            sign, SQL(operator), operand)
        ids = [row[0] for row in self.env.execute_query(sql)]
        return [('id', 'in', ids)] if ids else [('id', '=', '0')]
