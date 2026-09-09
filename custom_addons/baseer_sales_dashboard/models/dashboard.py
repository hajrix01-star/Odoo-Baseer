"""Read-only sales dashboard: existing daily report owns sales/day accounting."""
import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
from odoo.addons.baseer_pos_summary.models.common import money, ZERO, clean_context
from odoo.addons.baseer_pos_summary.models.operations import checked_range


# Explicit reporting budgets: never silently shorten an All time request.
MAX_REPORT_YEARS = 100
MAX_MONTH_POINTS = 1201
MAX_SOURCE_ROWS = 100000


def _display(value, integer=False):
    if value is None:
        return '—'
    return f'{value:,.0f}' if integer else f'{money(value):,.2f}'


def _month_start(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _previous_year(value):
    return value.replace(year=value.year - 1, day=min(value.day, calendar.monthrange(value.year - 1, value.month)[1]))


def _card(value, available=True, integer=False):
    available = bool(available and value is not None)
    return {'value': (int(value) if integer else str(money(value))) if available else None,
            'display': _display(value, integer) if available else '—', 'available': available}


class SalesDashboard(models.Model):
    _inherit = 'spreadsheet.dashboard'

    baseer_dashboard_kind = fields.Selection([('sales_summary', 'Sales summaries')], readonly=True, copy=False)

    def _baseer_source_bounds(self):
        source = self.env['baseer.pos.summary']
        source.check_access('read')
        domain = [('company_id', '=', self.env.company.id), ('state', '=', 'approved')]
        first = source.search(domain, order='business_date,id', limit=1)
        last = source.search(domain, order='business_date desc,id desc', limit=1)
        return first.business_date or None, last.business_date or None

    def _baseer_native_periods(self, value):
        """Resolve native DateValue intent in the supported company timezone."""
        today = fields.Date.context_today(self.with_context(tz='Asia/Riyadh'))
        first = last = previous_first = previous_last = None
        preset, force_month = 'all_time', False
        if value is None:
            first, last = self._baseer_source_bounds()
        else:
            if not isinstance(value, dict):
                raise ValidationError(_('Choose a valid native date filter.'))
            kind = value.get('type')
            allowed = {'relative': {'type', 'period'}, 'month': {'type', 'month', 'year'},
                       'quarter': {'type', 'quarter', 'year'}, 'year': {'type', 'year'},
                       'range': {'type', 'from', 'to'}}
            if not isinstance(kind, str) or kind not in allowed or set(value) - allowed[kind]:
                raise ValidationError(_('Choose a valid native date filter.'))
            try:
                if kind in ('month', 'quarter', 'year'):
                    year = value.get('year')
                    if type(year) is not int or not 1 <= year <= 9999:
                        raise ValueError
                    if kind == 'year':
                        first, last = date(year, 1, 1), date(year, 12, 31)
                        previous_first, previous_last = date(year - 1, 1, 1), date(year - 1, 12, 31)
                        force_month = True
                    else:
                        number = value.get(kind)
                        if type(number) is not int or not 1 <= number <= (12 if kind == 'month' else 4):
                            raise ValueError
                        width = 1 if kind == 'month' else 3
                        first = date(year, number if kind == 'month' else (number - 1) * 3 + 1, 1)
                        last = _month_start(first, width) - timedelta(days=1)
                        previous_first, previous_last = _month_start(first, -width), first - timedelta(days=1)
                    preset = 'native_' + kind
                elif kind == 'relative':
                    period = value.get('period')
                    lengths = {'today': 1, 'yesterday': 1, 'last_7_days': 7, 'last_30_days': 30, 'last_90_days': 90}
                    if not isinstance(period, str):
                        raise ValueError
                    if period in lengths:
                        last = today - timedelta(days=1) if period == 'yesterday' else today
                        first = last - timedelta(days=lengths[period] - 1)
                        previous_last = first - timedelta(days=1)
                        previous_first = previous_last - (last - first)
                    elif period == 'month_to_date':
                        first, last = today.replace(day=1), today
                        previous_first = _month_start(first, -1)
                        previous_last = previous_first.replace(day=min(today.day, calendar.monthrange(previous_first.year, previous_first.month)[1]))
                    elif period == 'last_month':
                        first, last = _month_start(today, -1), today.replace(day=1) - timedelta(days=1)
                        previous_first, previous_last = _month_start(first, -1), first - timedelta(days=1)
                    elif period == 'year_to_date':
                        first, last = today.replace(month=1, day=1), today
                        previous_first, previous_last = _previous_year(first), _previous_year(last)
                        force_month = True
                    elif period == 'last_12_months':
                        next_day = today + timedelta(days=1)
                        first = _month_start(next_day, -12)
                        last = next_day.replace(day=1) - timedelta(days=1)
                        previous_first, previous_last = _month_start(first, -12), first - timedelta(days=1)
                        force_month = True
                    else:
                        raise ValueError
                    preset = period
                else:
                    raw_from, raw_to = value.get('from', ''), value.get('to', '')
                    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
                        raise ValueError
                    if any(raw and len(raw) != 10 for raw in (raw_from, raw_to)):
                        raise ValueError
                    first = fields.Date.to_date(raw_from) if raw_from else None
                    last = fields.Date.to_date(raw_to) if raw_to else None
                    if first and last:
                        if first > last:
                            raise ValueError
                        previous_last = first - timedelta(days=1)
                        previous_first = previous_last - (last - first)
                    else:
                        bound_first, bound_last = self._baseer_source_bounds()
                        first, last = first or bound_first, last or bound_last
                        # No invented dates or negative spans when an open range
                        # lies beyond all known approved reporting history.
                        if not bound_first or not first or not last or first > last:
                            first = last = None
                    preset = 'native_range'
            except (TypeError, ValueError, OverflowError):
                raise ValidationError(_('Choose a valid native date filter.')) from None
        granularity = 'month' if force_month or (first and last and (last - first).days >= 31) else 'day'
        return preset, first, last, previous_first, previous_last, granularity

    def _baseer_check_capacity(self, company, periods):
        """Read-rule-aware, combined source budget before materializing days."""
        ranges = [(first, last) for first, last in periods if first and last]
        for first, last in ranges:
            end_year = first.year + MAX_REPORT_YEARS
            anniversary = date(end_year, first.month, min(first.day, calendar.monthrange(end_year, first.month)[1])) if end_year <= 9999 else None
            month_count = (last.year - first.year) * 12 + last.month - first.month + 1
            if last < first or (anniversary and last >= anniversary) or month_count > MAX_MONTH_POINTS:
                raise ValidationError(_('This report exceeds the supported 100-year date span. Choose a shorter period; no dates have been omitted.'))
        remaining = MAX_SOURCE_ROWS
        for model, start_field, end_field in [('baseer.pos.summary', 'business_date', 'business_date'),
                                               ('baseer.pos.closure', 'date_from', 'date_to'),
                                               ('baseer.pos.summary.allocation', 'summary_id.business_date', 'summary_id.business_date')]:
            source = self.env[model]
            source.check_access('read')
            payment_source = model == 'baseer.pos.summary.allocation'
            model_ranges = [(first, last) for first, last in periods[:1] if first and last] if payment_source else ranges
            if not model_ranges:
                continue
            # A single OR domain counts a closure spanning several chunks once.
            domain = [('summary_id.company_id' if payment_source else 'company_id', '=', company.id)] + ['|'] * (len(model_ranges) - 1)
            for first, last in model_ranges:
                domain += ['&', (start_field, '<=', last), (end_field, '>=', first)]
            count = source.search_count(domain, limit=remaining + 1)
            remaining -= count
            if remaining < 0:
                raise ValidationError(_('This report exceeds 100,000 source rows. Choose a shorter period; no records have been omitted.'))

    def _baseer_periods(self, filters):
        if isinstance(filters, dict) and 'native' in filters:
            if set(filters) != {'native'}:
                raise ValidationError(_('Choose valid dashboard filters.'))
            return self._baseer_native_periods(filters['native'])
        if not isinstance(filters, dict) or set(filters) - {'preset', 'date_from', 'date_to'}:
            raise ValidationError(_('Choose valid dashboard filters.'))
        preset = filters.get('preset', 'this_month')
        today = fields.Date.context_today(self.with_context(tz='Asia/Riyadh'))
        if preset == 'this_month':
            first, last = today.replace(day=1), today
            previous_first = _month_start(first, -1)
            previous_last = previous_first.replace(day=min(last.day, calendar.monthrange(previous_first.year, previous_first.month)[1]))
        elif preset == 'last_month':
            first, last = _month_start(today, -1), today.replace(day=1) - timedelta(days=1)
            previous_first, previous_last = _month_start(first, -1), first - timedelta(days=1)
        elif preset == 'this_year':
            first, last = today.replace(month=1, day=1), today
            previous_first, previous_last = _previous_year(first), _previous_year(last)
        elif preset == 'last_year':
            first, last = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
            previous_first, previous_last = date(today.year - 2, 1, 1), date(today.year - 2, 12, 31)
        elif preset == 'last_30_days':
            first, last = today - timedelta(days=29), today
            previous_first, previous_last = first - timedelta(days=30), first - timedelta(days=1)
        elif preset == 'custom':
            try:
                raw = [filters.get('date_from'), filters.get('date_to')]
                if any(not isinstance(value, str) or len(value) != 10 for value in raw):
                    raise ValueError
                first, last = checked_range(*raw)
                previous_last = first - timedelta(days=1)
                previous_first = previous_last - (last - first)
            except (TypeError, ValueError, OverflowError):
                raise ValidationError(_('Choose a valid date range of at most 366 days.')) from None
        else:
            raise ValidationError(_('Choose valid dashboard filters.'))
        checked_range(first, last)
        checked_range(previous_first, previous_last)
        granularity = 'month' if preset in ('this_year', 'last_year') or (last - first).days >= 31 else 'day'
        return preset, first, last, previous_first, previous_last, granularity

    def _baseer_aggregate_period(self, company, first, last):
        """Reuse every day decision; combine numerators, never chunk averages."""
        totals = dict(recorded_sales=ZERO, recorded_customers=0, complete_sales=ZERO, complete_customers=0,
                      operating_days=0, closed_days=0, incomplete_days=0, missing_days=0)
        days = []
        cursor = first
        while cursor and last and cursor <= last:
            end = min(last, cursor + timedelta(days=min(365, (last - cursor).days)))
            chunk = self.env['baseer.pos.daily.report']._aggregate_days(company, cursor, end)
            for key in totals:
                totals[key] += chunk['totals'][key]
            days.extend(chunk['days'])
            if end == last:
                break
            cursor = end + timedelta(days=1)
        denominator = totals['operating_days']
        totals['average_daily_sales'] = money(totals['complete_sales'] / Decimal(denominator)) if denominator else ZERO
        totals['average_daily_customers'] = money(Decimal(totals['complete_customers']) / Decimal(denominator)) if denominator else ZERO
        return {'totals': totals, 'days': days}

    def _baseer_metrics(self, company, first, last):
        report = self.env['baseer.pos.daily.report']
        report.check_access('read')
        self.env['baseer.pos.summary'].check_access('read')
        self.env['baseer.pos.closure'].check_access('read')
        data = self._baseer_aggregate_period(company, first, last)
        # A positive sale without a customer count cannot support a reliable
        # customer-based ratio. No second implementation of daily accounting.
        missing = self.env['baseer.pos.summary'].search([
            ('company_id', '=', company.id), ('business_date', '>=', first),
            ('business_date', '<=', last), ('state', '=', 'approved'),
            ('amount_gross', '>', 0), ('customer_count', '=', 0),
        ]) if first and last else self.env['baseer.pos.summary']
        complete_dates = {row['business_date'] for row in data['days'] if row['status'] == 'complete'}
        missing_complete = sum(item.business_date in complete_dates for item in missing)
        totals = data['totals']
        cards = {
            'sales': _card(totals['recorded_sales']),
            'customers': _card(totals['recorded_customers'], integer=True),
            'daily_sales': _card(totals['average_daily_sales'], totals['operating_days'] > 0),
            'daily_customers': _card(totals['average_daily_customers'], totals['operating_days'] > 0 and not missing_complete),
            'average_bill': _card(totals['recorded_sales'] / Decimal(totals['recorded_customers'])
                                  if totals['recorded_customers'] else None, not missing),
        }
        return {'data': data, 'cards': cards, 'has_sample': any(row['has_sales'] for row in data['days']),
                'issues': {'missing_customers': len(missing), 'missing_complete_customers': missing_complete}}

    def _baseer_source_action(self, company, first, last, period_scope=None):
        domain = [('company_id', '=', company.id), ('state', '=', 'approved')]
        domain += [('business_date', '>=', first.isoformat()), ('business_date', '<=', last.isoformat())] if first and last else [('id', '=', False)]
        if period_scope:
            domain.append(('period_scope', '=', period_scope))
        return {'type': 'ir.actions.act_window', 'name': _('Approved sales summaries'),
                'res_model': 'baseer.pos.summary', 'view_mode': 'list,form',
                'views': [(False, 'list'), (False, 'form')], 'domain': domain,
                'context': {'create': False, 'allowed_company_ids': [company.id]}}

    def _baseer_shift_performance(self, company, first, last):
        source = self.env['baseer.pos.summary']
        records = source.search(self._baseer_source_action(company, first, last)['domain']) if first and last else source
        rows = []
        for key, label in [('morning', _('Morning')), ('evening', _('Evening')), ('all', _('Full day'))]:
            selected = records.filtered(lambda row: row.period_scope == key)
            sales = sum((money(row.amount_gross) for row in selected), ZERO)
            customers = sum(selected.mapped('customer_count'))
            missing = len(selected.filtered(lambda row: row.amount_gross > 0 and not row.customer_count))
            rows.append({'key': key, 'label': label, 'has_data': bool(selected), 'summary_count': len(selected),
                         'missing_customers': missing,
                         'cards': {'sales': _card(sales, bool(selected)),
                                   'customers': _card(customers, bool(selected), integer=True),
                                   'average_bill': _card(sales / Decimal(customers) if customers else None, bool(selected) and not missing)},
                         'source_action': self._baseer_source_action(company, first, last, key)})
        return {'rows': rows}

    def _baseer_payment_performance(self, company, first, last, recorded_sales):
        """Declared sales allocations only; never bank/platform settlement."""
        source = self.env['baseer.pos.summary']
        records = source.search(self._baseer_source_action(company, first, last)['domain']) if first and last else source
        Allocation = self.env['baseer.pos.summary.allocation']
        Allocation.check_access('read')
        domain = [('summary_id.company_id', '=', company.id), ('summary_id.state', '=', 'approved')]
        if first and last:
            domain += [('summary_id.business_date', '>=', first), ('summary_id.business_date', '<=', last)]
            allocations = Allocation.search(domain)
        else:
            allocations = Allocation
        by_summary = defaultdict(list)
        for line in allocations:
            by_summary[line.summary_id.id].append(line)
        missing = mismatched = covered_count = 0
        covered_sales = ZERO
        by_method = {}
        for record in records:
            lines = by_summary[record.id]
            gross = money(record.amount_gross)
            if gross > ZERO and not lines:
                missing += 1
                continue
            valid = True
            amounts = []
            for line in lines:
                amount = Decimal(str(line.amount))
                method = line.payment_method_id
                category = method.baseer_category_id
                if (not amount.is_finite() or amount < ZERO or amount != money(amount)
                        or method.company_id != company or not category or category.company_id != company):
                    valid = False
                    break
                amounts.append(money(amount))
            if not valid or sum(amounts, ZERO) != gross:
                mismatched += 1
                continue
            covered_count += 1
            covered_sales += gross
            for line, amount in zip(lines, amounts):
                method = line.payment_method_id
                bucket = by_method.setdefault(method.id, {'method': method, 'sales': ZERO, 'summary_ids': []})
                bucket['sales'] += amount
                bucket['summary_ids'].append(record.id)
        complete = covered_count == len(records)
        rows = []
        by_category = {}
        for method_id, bucket in sorted(by_method.items()):
            method = bucket['method']
            category = method.baseer_category_id
            category_bucket = by_category.setdefault(category.id, {
                'name': category.name, 'kind': category.kind, 'sales': ZERO})
            category_bucket['sales'] += bucket['sales']
            action = {'type': 'ir.actions.act_window', 'name': _('Approved payment allocations'),
                      'res_model': 'baseer.pos.summary.allocation', 'view_mode': 'list,form',
                      'views': [(False, 'list'), (False, 'form')],
                      'domain': domain + [('payment_method_id', '=', method_id), ('summary_id', 'in', bucket['summary_ids'])],
                      'context': {'create': False, 'allowed_company_ids': [company.id]}}
            rows.append({'method_id': method_id, 'name': method.name,
                         'category': {'id': category.id, 'name': category.name, 'kind': category.kind},
                         'sales': _card(bucket['sales']),
                         'share': _card(bucket['sales'] * Decimal(100) / recorded_sales if recorded_sales > ZERO else None, complete),
                         'source_action': action})
        categories = [{'category_id': category_id, 'name': bucket['name'], 'kind': bucket['kind'],
                       'sales': _card(bucket['sales'])}
                      for category_id, bucket in sorted(by_category.items())]
        application_sales = sum((bucket['sales'] for bucket in by_category.values()
                                 if bucket['kind'] == 'platform'), ZERO)
        application_share = _card(application_sales * Decimal(100) / recorded_sales
                                  if recorded_sales > ZERO else None, complete)
        return {'rows': rows, 'categories': categories, 'application_share': application_share,
                'coverage': {'complete': complete, 'summary_count': len(records),
                'covered_summary_count': covered_count, 'missing_summary_count': missing,
                'mismatched_summary_count': mismatched, 'covered_sales': _card(covered_sales),
                'uncovered_sales': _card(recorded_sales - covered_sales)}}

    def _baseer_comparison(self, current, previous, has_sample):
        result = {'direction': 'none', 'display': _('Not available'), 'available': False,
                  'previous_display': previous['display']}
        if not has_sample or not current['available'] or not previous['available']:
            return result
        now, before = Decimal(str(current['value'])), Decimal(str(previous['value']))
        result['available'] = True
        if not before and now:
            result.update(direction='new', display=_('New'))
        else:
            percentage = money((now - before) * Decimal(100) / abs(before)) if before else ZERO
            result.update(direction='up' if now > before else 'down' if now < before else 'flat',
                          display=('+' if percentage > 0 else '') + _display(percentage) + '%')
        return result

    def _baseer_timeline(self, days, granularity):
        buckets = {}
        for day in days:
            key = day['date_label'][:7] if granularity == 'month' else day['date_label']
            bucket = buckets.setdefault(key, dict(key=key, label=key, sales=ZERO, customers=0, has_sample=False,
                                                  operating_days=0, incomplete_days=0, closed_days=0, missing_days=0))
            bucket[{'complete': 'operating_days', 'incomplete': 'incomplete_days', 'closed': 'closed_days', 'missing': 'missing_days'}[day['status']]] += 1
            if day['has_sales']:
                bucket['has_sample'] = True
                bucket['sales'] += money(day['sales'])
                bucket['customers'] += day['customers']
        points = []
        for bucket in buckets.values():
            sample = bucket.pop('has_sample')
            status = ('incomplete' if bucket['incomplete_days'] or bucket['missing_days'] else 'complete') if sample else (
                'closed' if bucket['closed_days'] and not bucket['missing_days'] else 'missing')
            points.append(dict(bucket, status=status,
                               sales=str(money(bucket['sales'])) if sample else None,
                               sales_display=_display(bucket['sales']) if sample else '—',
                               customers=bucket['customers'] if sample else None,
                               customers_display=_display(bucket['customers'], True) if sample else '—'))
        return {'granularity': granularity, 'points': points}

    @api.readonly
    def get_baseer_sales_metrics(self, filters=None):
        self.ensure_one()
        self.check_access('read')
        if self.baseer_dashboard_kind != 'sales_summary' or not self.is_published:
            raise AccessError(_('This sales dashboard is not available.'))
        company = self.env.company
        if self.company_ids and company not in self.company_ids:
            raise AccessError(_('This dashboard is not available for the active company.'))
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessError(_('Point of Sale access is required to view sales summaries.'))
        scoped = clean_context(self, company)
        preset, first, last, previous_first, previous_last, granularity = scoped._baseer_periods({} if filters is None else filters)
        scoped._baseer_check_capacity(company, [(first, last), (previous_first, previous_last)])
        current = scoped._baseer_metrics(company, first, last)
        previous = scoped._baseer_metrics(company, previous_first, previous_last)
        for key, card in current['cards'].items():
            card['comparison'] = scoped._baseer_comparison(card, previous['cards'][key], current['has_sample'] and previous['has_sample'])
        totals = current['data']['totals']
        return {
            'company': {'id': company.id, 'name': company.name, 'currency': company.currency_id.name},
            'filters': {'preset': preset, 'date_from': first.isoformat() if first else None, 'date_to': last.isoformat() if last else None, 'granularity': granularity},
            'comparison_period': {'date_from': previous_first.isoformat() if previous_first else None,
                                  'date_to': previous_last.isoformat() if previous_last else None},
            'cards': current['cards'],
            'coverage': {key: totals[key] for key in ('operating_days', 'incomplete_days', 'closed_days', 'missing_days')},
            'issues': current['issues'], 'has_data': current['has_sample'],
            'timeline': scoped._baseer_timeline(current['data']['days'], granularity),
            'source_action': scoped._baseer_source_action(company, first, last),
            'shift_performance': scoped._baseer_shift_performance(company, first, last),
            'payment_performance': scoped._baseer_payment_performance(company, first, last, totals['recorded_sales']),
        }
