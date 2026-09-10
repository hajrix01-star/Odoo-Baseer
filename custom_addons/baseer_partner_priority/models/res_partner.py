"""Company preferences and rule-filtered ranking; native bills remain authoritative."""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import SQL


class ResPartner(models.Model):
    _inherit = 'res.partner'

    baseer_is_favorite = fields.Boolean(
        string='Company Favorite', company_dependent=True, default=False, copy=False,
        help='Preferred in the active company. Other companies keep their own favorites.',
    )
    baseer_recent_bill_count = fields.Integer(
        string='Recent Vendor Bills', compute='_compute_recent_bill_count',
        help='Posted vendor bills in the last 90 days, in the active company and within your access.',
    )

    def _baseer_priority_enabled(self):
        return (self.env.context.get('baseer_partner_priority') or
                self.env.context.get('res_partner_search_mode') in ('supplier', 'customer'))

    @property
    def _order(self):
        if self._baseer_priority_enabled():
            return 'baseer_is_favorite DESC, baseer_recent_bill_count DESC, name ASC, id ASC'
        return super()._order

    @api.model
    def name_search(self, name='', domain=None, operator='ilike', limit=100):
        if self._baseer_priority_enabled():
            domain = fields.Domain(domain or []) & fields.Domain('company_id', 'in', [False, self.env.company.id])
        return super().name_search(name, domain, operator, limit)

    @api.model
    def web_search_read(self, domain, specification, offset=0, limit=None, order=None, count_limit=None):
        if self._baseer_priority_enabled():
            domain = fields.Domain(domain) & fields.Domain('company_id', 'in', [False, self.env.company.id])
        return super().web_search_read(domain, specification, offset, limit, order, count_limit)

    def _baseer_recent_bill_domain(self):
        plain = self.with_context(baseer_partner_priority=False, res_partner_search_mode=False)
        today = fields.Date.context_today(plain)
        return [
            ('company_id', '=', self.env.company.id),
            ('move_type', '=', 'in_invoice'), ('state', '=', 'posted'),
            ('invoice_date', '>=', today - timedelta(days=89)),
            ('invoice_date', '<=', today),
        ]

    @api.model
    def web_name_search(self, name, specification, domain=None, operator='ilike', limit=100):
        values = super().web_name_search(name, specification, domain, operator, limit)
        # Adding favorite metadata must not disable native multiline/address labels.
        if self._baseer_priority_enabled() and 'baseer_is_favorite' in specification:
            records = self.browse([row['id'] for row in values]).with_context(formatted_display_name=True)
            labels = {record.id: record.display_name for record in records}
            for row in values:
                row['__formatted_display_name'] = labels[row['id']]
        return values

    @api.depends_context('company', 'uid', 'tz')
    def _compute_recent_bill_count(self):
        counts = {}
        moves = self.env['account.move'].with_context(baseer_partner_priority=False, res_partner_search_mode=False)
        if moves.has_access('read'):
            counts = {partner.id: count for partner, count in moves._read_group(
                self._baseer_recent_bill_domain() + [('partner_id', 'in', self.ids)],
                ['partner_id'], ['__count'],
            )}
        for partner in self:
            partner.baseer_recent_bill_count = counts.get(partner.id, 0)

    def _order_field_to_sql(self, alias, field_name, direction, nulls, query):
        if field_name != 'baseer_recent_bill_count':
            return super()._order_field_to_sql(alias, field_name, direction, nulls, query)
        # Validate the active company even if the user cannot read bills.
        domain = self._baseer_recent_bill_domain()
        moves = self.env['account.move'].with_context(baseer_partner_priority=False, res_partner_search_mode=False)
        if not moves.has_access('read'):
            return SQL('0 + 0 %s', direction)
        # _search includes native bill access rules; no sudo or raw-table shortcut.
        bill_query = moves._search(domain)
        visible_bills = bill_query.select(moves._field_to_sql('account_move', 'partner_id', bill_query))
        grouped = SQL('(SELECT partner_id, COUNT(*) AS bill_count FROM (%s) AS visible_bills GROUP BY partner_id)', visible_bills)
        usage_alias = query.make_alias(alias, 'baseer_recent_bills')
        query.add_join('LEFT JOIN', usage_alias, grouped,
                       SQL('%s = %s', SQL.identifier(usage_alias, 'partner_id'), SQL.identifier(alias, 'id')))
        count = SQL('COALESCE(%s, 0)', SQL.identifier(usage_alias, 'bill_count'))
        query._order_groupby.append(count)
        return SQL('%s %s %s', count, direction, nulls)

    def _baseer_check_favorite_company(self):
        company = self.env.company
        self.check_access('write')
        if any(partner.company_id and partner.company_id != company for partner in self):
            raise AccessError(_('Switch to the contact company before changing its favorite.'))

    def write(self, vals):
        if 'baseer_is_favorite' in vals:
            self._baseer_check_favorite_company()
            if vals.get('company_id') and vals['company_id'] != self.env.company.id:
                raise AccessError(_('Switch to the contact company before changing its favorite.'))
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if (any('baseer_is_favorite' in vals for vals in vals_list) or
                'default_baseer_is_favorite' in self.env.context):
            records._baseer_check_favorite_company()
        return records
