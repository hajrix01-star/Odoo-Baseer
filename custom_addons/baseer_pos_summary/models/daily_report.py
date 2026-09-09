from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError

from .common import ZERO, money, active_company, clean_context, internal, INTERNAL_TOKEN, TOKEN_KEY
from .operations import checked_range, covered_slots


class PosDailyReport(models.TransientModel):
    _name = 'baseer.pos.daily.report'
    _description = 'Daily sales and customers report'

    name = fields.Char(default=lambda self: _('Daily sales and customers'), readonly=True)

    company_id = fields.Many2one('res.company', required=True, readonly=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    date_from = fields.Date(required=True, default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    generated = fields.Boolean(readonly=True)
    recorded_sales = fields.Monetary(readonly=True)
    recorded_customers = fields.Integer(readonly=True)
    complete_sales = fields.Monetary(readonly=True)
    complete_customers = fields.Integer(readonly=True)
    average_daily_sales = fields.Monetary(readonly=True)
    average_daily_customers = fields.Float(readonly=True, digits=(16, 2))
    operating_days = fields.Integer(readonly=True)
    closed_days = fields.Integer(readonly=True)
    incomplete_days = fields.Integer(readonly=True)
    missing_days = fields.Integer(readonly=True)
    line_ids = fields.One2many('baseer.pos.daily.report.line', 'report_id', readonly=True)
    _INPUTS = {'company_id', 'date_from', 'date_to'}

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if set(vals) - self._INPUTS:
                raise AccessError(_('Report results are calculated by the server.'))
            active_company(self, self.env['res.company'].browse(vals.get('company_id') or self.env.company.id))
        scoped = clean_context(self, self.env.company)
        return super(PosDailyReport, scoped).create([dict(vals, company_id=self.env.company.id) for vals in vals_list])

    def _authorize(self):
        self.check_access('read')
        for report in self:
            active_company(report, report.company_id)
            if report.create_uid != self.env.user:
                raise AccessError(_('Only the report creator can use these results.'))

    def write(self, vals):
        self._authorize()
        if set(vals) - {'date_from', 'date_to'}:
            raise AccessError(_('Report results are calculated by the server.'))
        result = super().write(dict(vals, generated=False))
        self.line_ids.with_context(**{TOKEN_KEY: INTERNAL_TOKEN}).unlink()
        return result

    @api.model
    def _aggregate_days(self, company, date_from, date_to):
        """Single monetary/operating-day authority for report and future dashboards."""
        active_company(self, company)
        first, last = checked_range(date_from, date_to)
        summaries = self.env['baseer.pos.summary'].search([('company_id', '=', company.id), ('business_date', '>=', first), ('business_date', '<=', last), ('state', '!=', 'cancelled')])
        closures = self.env['baseer.pos.closure'].search([('company_id', '=', company.id), ('state', '=', 'confirmed'), ('date_from', '<=', last), ('date_to', '>=', first)])
        by_day, by_closed = defaultdict(list), defaultdict(list)
        for summary in summaries:
            by_day[summary.business_date].append(summary)
        for closure in closures:
            date = max(first, closure.date_from)
            while date <= min(last, closure.date_to):
                by_closed[date].append(closure)
                date += timedelta(days=1)
        totals = dict(recorded_sales=ZERO, recorded_customers=0, complete_sales=ZERO, complete_customers=0,
                      operating_days=0, closed_days=0, incomplete_days=0, missing_days=0)
        rows, date = [], first
        while date <= last:
            day_summaries = by_day[date]
            approved = [row for row in day_summaries if row.state == 'approved']
            closed = set().union(*(covered_slots(row.period_scope) for row in by_closed[date]))
            represented = set().union(*(covered_slots(row.period_scope) for row in approved))
            schedule = day_summaries[0].day_schedule if day_summaries else 'split'
            expected = {'morning', 'evening'} if schedule in ('all', 'split') else {schedule}
            sales = sum((money(row.amount_gross) for row in approved), ZERO)
            customers = sum(row.customer_count for row in approved)
            if closed == {'morning', 'evening'} and not approved:
                status = 'closed'
            elif approved and expected.issubset(represented | closed):
                status = 'complete'
            elif approved:
                status = 'incomplete'
            else:
                status = 'missing'
            totals['recorded_sales'] += sales
            totals['recorded_customers'] += customers
            totals[{'complete': 'operating_days', 'closed': 'closed_days', 'incomplete': 'incomplete_days', 'missing': 'missing_days'}[status]] += 1
            if status == 'complete':
                totals['complete_sales'] += sales
                totals['complete_customers'] += customers
            rows.append(dict(business_date=date, date_label=date.isoformat(), status=status, has_sales=bool(approved), sales=float(sales),
                             customers=customers, summary_ids=[row.id for row in day_summaries],
                             closure_ids=[row.id for row in by_closed[date]]))
            date += timedelta(days=1)
        denominator = totals['operating_days']
        totals['average_daily_sales'] = money(totals['complete_sales'] / Decimal(denominator)) if denominator else ZERO
        totals['average_daily_customers'] = money(Decimal(totals['complete_customers']) / Decimal(denominator)) if denominator else ZERO
        return {'totals': totals, 'days': rows}

    def action_generate(self):
        self.ensure_one()
        self._authorize()
        data = self._aggregate_days(self.company_id, self.date_from, self.date_to)
        values = {key: float(value) if isinstance(value, Decimal) else value for key, value in data['totals'].items()}
        lines = []
        for source in data['days']:
            row = dict(source, company_id=self.company_id.id)
            row['summary_ids'] = [Command.set(row['summary_ids'])]
            row['closure_ids'] = [Command.set(row['closure_ids'])]
            lines.append(Command.create(row))
        scoped = self.with_context(**{TOKEN_KEY: INTERNAL_TOKEN})
        super(PosDailyReport, scoped).write(dict(values, generated=True, line_ids=[Command.clear()] + lines))
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'current'}

    def action_timeline(self):
        self.ensure_one()
        self._authorize()
        return {'type': 'ir.actions.act_window', 'name': _('Completed operating days — sales and customers'),
                'res_model': 'baseer.pos.daily.report.line', 'view_mode': 'graph,list',
                'domain': [('report_id', '=', self.id), ('company_id', '=', self.company_id.id), ('status', '=', 'complete')],
                'context': {'graph_measure': 'sales', 'graph_groupbys': ['date_label'], 'create': False, 'edit': False, 'delete': False}}


class PosDailyReportLine(models.TransientModel):
    _name = 'baseer.pos.daily.report.line'
    _description = 'Daily sales result'
    _order = 'business_date'
    _rec_name = 'date_label'

    report_id = fields.Many2one('baseer.pos.daily.report', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', required=True, readonly=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    business_date = fields.Date(readonly=True, index=True)
    # A categorical ISO date prevents native temporal gap filling from inventing
    # zero sales for dates intentionally excluded as missing or closed.
    date_label = fields.Char(readonly=True, string='Operating date', index=True)
    status = fields.Selection([('complete', 'Complete operating day'), ('incomplete', 'Incomplete day'), ('closed', 'Closed'), ('missing', 'Missing entry')], readonly=True)
    has_sales = fields.Boolean(readonly=True)
    sales = fields.Monetary(readonly=True, string='Sales including VAT')
    customers = fields.Integer(readonly=True, string='Customers')
    summary_ids = fields.Many2many('baseer.pos.summary', readonly=True)
    closure_ids = fields.Many2many('baseer.pos.closure', readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not internal(self):
            raise AccessError(_('Daily result rows can only be generated by the report.'))
        return super().create(vals_list)

    def write(self, vals):
        if not internal(self):
            raise AccessError(_('Daily result rows cannot be edited.'))
        return super().write(vals)

    def unlink(self):
        if not internal(self):
            raise AccessError(_('Daily result rows cannot be deleted manually.'))
        return super().unlink()

    def _transient_clean_rows_older_than(self, seconds):
        # Native vacuum is a private server path; a JSON context cannot forge
        # this identity token. Manual mutation of result rows remains blocked.
        scoped = self.with_context(**{TOKEN_KEY: INTERNAL_TOKEN})
        return super(PosDailyReportLine, scoped)._transient_clean_rows_older_than(seconds)

    def _source_action(self, model, records, title):
        self.ensure_one()
        self.check_access('read')
        self.report_id._authorize()
        records.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': title, 'res_model': model, 'view_mode': 'list,form',
                'domain': [('id', 'in', records.ids), ('company_id', '=', self.company_id.id)],
                'context': {'create': False}}

    def action_summaries(self):
        return self._source_action('baseer.pos.summary', self.summary_ids, _('Sales summaries'))

    def action_closures(self):
        return self._source_action('baseer.pos.closure', self.closure_ids, _('Operating closures'))
