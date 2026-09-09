"""Read-only daily projection of the original per-shift sales summaries."""
from collections import defaultdict
from datetime import date
from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError

from .common import active_company


class PosDayArchive(models.Model):
    _name = 'baseer.pos.day.archive'
    _description = 'Daily sales summaries'
    _auto = False
    _log_access = False
    _order = 'business_date desc, id desc'
    _rec_name = 'business_date'

    company_id = fields.Many2one('res.company', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    business_date = fields.Date(readonly=True)
    day_schedule = fields.Selection([('all', 'All day'), ('split', 'Morning and evening'), ('morning', 'Morning only'), ('evening', 'Evening only')], readonly=True)
    amount_total = fields.Monetary(readonly=True, string='Sales including VAT')
    customer_total = fields.Integer(readonly=True, string='Customers')
    shift_count = fields.Integer(readonly=True, string='Recorded periods')
    is_day_off = fields.Boolean(readonly=True, string='Closed day')
    closure_id = fields.Many2one('baseer.pos.closure', readonly=True)
    is_archived = fields.Boolean(readonly=True, string='Archived')
    morning_total = fields.Monetary(readonly=True, string='Morning sales')
    evening_total = fields.Monetary(readonly=True, string='Evening sales')
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('mixed', 'Partly approved'), ('closed', 'Closed')], readonly=True)
    missing_shift = fields.Boolean(compute='_compute_coverage', search='_search_missing_shift', string='Missing shift')

    def _compute_coverage(self):
        groups = defaultdict(lambda: self.browse())
        for day in self:
            groups[(day.company_id, day.business_date.year)] |= day
        for (company, year), days in groups.items():
            report = self.env['baseer.pos.daily.report'].with_company(company)
            coverage = report._aggregate_days(company, date(year, 1, 1), date(year, 12, 31))
            statuses = {row['business_date']: row['status'] for row in coverage['days']}
            for day in days:
                day.missing_shift = statuses[day.business_date] in ('missing', 'incomplete')

    @api.model
    def _search_missing_shift(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            raise UserError(_('Choose whether a day has a missing shift.'))
        # One company, bounded archive dates, batched through the same authority.
        days = self.search([('company_id', '=', self.env.company.id)], limit=5001)
        if len(days) > 5000:
            raise UserError(_('The missing-shift filter supports at most 5000 saved dates. Use the daily report for a bounded period.'))
        missing = days.filtered('missing_shift').ids
        return [('id', 'in' if (value == (operator == '=')) else 'not in', missing)]

    def action_view_closures(self):
        self.ensure_one()
        self.check_access('read')
        active_company(self, self.company_id)
        closures = self.env['baseer.pos.closure'].search([('company_id', '=', self.company_id.id),
            ('state', '=', 'confirmed'), ('date_from', '<=', self.business_date), ('date_to', '>=', self.business_date)])
        return {'type': 'ir.actions.act_window', 'name': _('Operating closures'), 'res_model': 'baseer.pos.closure',
                'view_mode': 'list,form', 'domain': [('id', 'in', closures.ids)], 'context': {'create': False}}

    @api.depends('business_date')
    def _compute_display_name(self):
        for day in self:
            day.display_name = _('Sales summaries — %s', fields.Date.to_string(day.business_date))

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE VIEW baseer_pos_day_archive AS
            SELECT MIN(summary.id)::bigint AS id,
                   summary.company_id,
                   company.currency_id,
                   summary.business_date,
                   MIN(summary.day_schedule) AS day_schedule,
                   SUM(COALESCE(summary.amount_gross, 0)::numeric) AS amount_total,
                   SUM(summary.customer_count)::integer AS customer_total,
                   COUNT(*)::integer AS shift_count,
                   SUM(CASE WHEN summary.period_scope = 'morning'
                            THEN COALESCE(summary.amount_gross, 0)::numeric ELSE 0::numeric END) AS morning_total,
                   SUM(CASE WHEN summary.period_scope = 'evening'
                            THEN COALESCE(summary.amount_gross, 0)::numeric ELSE 0::numeric END) AS evening_total,
                   CASE WHEN BOOL_AND(summary.state = 'approved') THEN 'approved'
                        WHEN BOOL_AND(summary.state = 'draft') THEN 'draft'
                        ELSE 'mixed' END AS state,
                   FALSE AS is_day_off,
                   NULL::integer AS closure_id,
                   BOOL_AND(COALESCE(summary.is_archived, FALSE)) AS is_archived
              FROM baseer_pos_summary AS summary
              JOIN res_company AS company ON company.id = summary.company_id
             WHERE summary.state != 'cancelled'
             GROUP BY summary.company_id, company.currency_id, summary.business_date
            UNION ALL
            SELECT -(closure.id::bigint * 1000 + day_offset)::bigint AS id,
                   closure.company_id,
                   company.currency_id,
                   closure.date_from + day_offset AS business_date,
                   'all' AS day_schedule,
                   NULL::numeric AS amount_total,
                   NULL::integer AS customer_total,
                   NULL::integer AS shift_count,
                   NULL::numeric AS morning_total,
                   NULL::numeric AS evening_total,
                   'closed' AS state,
                   TRUE AS is_day_off,
                   closure.id AS closure_id,
                   closure.is_archived AS is_archived
              FROM baseer_pos_closure AS closure
              JOIN res_company AS company ON company.id = closure.company_id
             CROSS JOIN LATERAL generate_series(0, LEAST(closure.date_to - closure.date_from, 365)) AS day_offset
             WHERE closure.state = 'confirmed' AND closure.period_scope = 'all'
        """)

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_('The daily archive is read-only. Use the sales entry form to create summaries.'))

    def write(self, vals):
        raise AccessError(_('The daily archive is read-only. Edit an original draft summary instead.'))

    def unlink(self):
        raise AccessError(_('The daily archive is read-only. Original summaries are retained for audit.'))

    def _get_sources(self):
        self.check_access('read')
        if not self or len(self.exists()) != len(self):
            raise UserError(_('Some selected days no longer exist. Refresh the summaries list.'))
        for day in self:
            active_company(day, day.company_id)
        sales_days = self.filtered(lambda day: not day.is_day_off)
        summaries = self.env['baseer.pos.summary'].search([
            ('company_id', '=', self.env.company.id), ('business_date', 'in', sales_days.mapped('business_date')), ('state', '!=', 'cancelled')])
        closures = self.closure_id.exists()
        summaries.check_access('read')
        closures.check_access('read')
        if (set(summaries.mapped('business_date')) != set(sales_days.mapped('business_date'))
                or any(closure.state != 'confirmed' for closure in closures)):
            raise UserError(_('Some selected days changed. Refresh the summaries list.'))
        return summaries, closures

    def _locked_sources(self, deleting=False):
        if not self.env.user.has_group('point_of_sale.group_pos_manager'):
            raise AccessError(_('Only a POS manager can archive, restore or delete daily summaries.'))
        summaries, closures = self._get_sources()
        summaries._lock('unlink' if deleting else 'write')
        closures._lock()
        if summaries:
            summaries[:1]._serialize_company()
        # The same company mutex used by create/approve prevents a concurrently
        # added shift from escaping a whole-day lifecycle action after retry.
        return self._get_sources()

    def action_archive(self):
        with self.env.cr.savepoint():
            summaries, closures = self._locked_sources()
            summaries.action_archive()
            closures.action_archive()
            self.invalidate_recordset(['is_archived'])
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_unarchive(self):
        with self.env.cr.savepoint():
            summaries, closures = self._locked_sources()
            summaries.action_unarchive()
            closures.action_unarchive()
            self.invalidate_recordset(['is_archived'])
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_delete_drafts(self):
        with self.env.cr.savepoint():
            summaries, closures = self._locked_sources(deleting=True)
            if closures or any(summary.state != 'draft' for summary in summaries):
                raise UserError(_('Only days containing draft sales summaries can be deleted. Approved sales and confirmed closures must be retained.'))
            summaries.unlink()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_open(self):
        self.ensure_one()
        self.check_access('read')
        active_company(self, self.company_id)
        if self.is_day_off:
            self.closure_id.check_access('read')
            return self.env['baseer.pos.day.entry']._from_closure(self.closure_id)._form_action()
        summaries = self.env['baseer.pos.summary'].search([
            ('company_id', '=', self.company_id.id), ('business_date', '=', self.business_date), ('state', '!=', 'cancelled')])
        summaries.check_access('read')
        if not summaries:
            raise UserError(_('This day no longer has sales summaries. Refresh the archive.'))
        return self.env['baseer.pos.day.entry']._from_summaries(summaries)._form_action()
