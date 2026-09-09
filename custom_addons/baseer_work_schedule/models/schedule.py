import hashlib
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

import pytz
from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, ValidationError
from .time_math import minutes, hhmm, compile_periods, daily_average_label

_TOKEN = object()


def trusted(record):
    return record.env.context.get('_baseer_schedule_change') is _TOKEN


def manager(record):
    if not record.env.user.has_group('hr.group_hr_manager'):
        raise AccessError(_('Only a Human Resources manager can set work schedules.'))


def lock_employees(employees):
    # Native self-service leave users need not read private hr.employee records.
    # Elevation is restricted to lock metadata; the caller's original environment
    # still performs every attendance/leave create/write and its native ACL checks.
    employees = employees.sudo()
    employees._baseer_lock_profile()
    for employee in employees.sorted(lambda e: (e.company_id.id, e.id)):
        # Touch the tuple, not its business values: stale REPEATABLE READ callers
        # serialize/retry instead of trusting an old snapshot after an advisory wait.
        employees.env.cr.execute('UPDATE hr_employee SET write_date=write_date WHERE id=%s', [employee.id])
    employees.invalidate_recordset(['version_ids', 'current_version_id'])


def lock_for_create(records, vals_list):
    records.check_access('create')
    default_employee = records.default_get(['employee_id']).get('employee_id') if any('employee_id' not in v for v in vals_list) else None
    for vals in vals_list:
        if 'employee_id' not in vals:
            vals['employee_id'] = default_employee
    lock_employees(records.env['hr.employee'].browse(list({v['employee_id'] for v in vals_list if v.get('employee_id')})))


def touch_calendars(calendars):
    # Employee locks precede tuple touches, including native membership changes.
    calendars.flush_recordset()
    for calendar_id in sorted(set(calendars.ids)):
        calendars.env.cr.execute('UPDATE resource_calendar SET write_date=write_date WHERE id=%s', [calendar_id])
    calendars.invalidate_recordset()


class Day(models.Model):
    _name = 'baseer.schedule.day'
    _description = 'Work Schedule Weekday'
    _order = 'sequence, id'
    name = fields.Char(required=True, translate=True)
    weekday = fields.Integer(required=True)
    sequence = fields.Integer()


class Calendar(models.Model):
    _inherit = 'resource.calendar'
    baseer_simple = fields.Boolean(copy=False, readonly=True)
    baseer_is_template = fields.Boolean(string='Work schedule template', copy=False)
    baseer_superseded_by_id = fields.Many2one('resource.calendar', string='Latest work schedule', copy=False, readonly=True, ondelete='restrict')
    baseer_effective_date = fields.Date(string='Applies from', copy=False, readonly=True)
    baseer_schedule_summary = fields.Text(compute='_compute_baseer_schedule_summary', string='Working times')
    baseer_average_label = fields.Char(compute='_compute_baseer_schedule_summary', string='Average working day (HH:MM)')

    @api.depends('attendance_ids.hour_from', 'attendance_ids.hour_to', 'attendance_ids.dayofweek', 'baseer_simple',
                 'attendance_ids.baseer_origin_day', 'attendance_ids.baseer_period_start',
                 'attendance_ids.baseer_period_end', 'hours_per_day')
    @api.depends_context('lang')
    def _compute_baseer_schedule_summary(self):
        for calendar in self:
            periods = defaultdict(set)
            for row in calendar._get_global_attendances():
                if calendar.baseer_simple:
                    periods[row.baseer_origin_day].add((row.baseer_period_start, row.baseer_period_end))
                else:
                    periods[int(row.dayofweek)].add((round(row.hour_from * 60), round(row.hour_to * 60)))
            if calendar.baseer_simple and periods:
                calendar.baseer_average_label = daily_average_label({
                    day: sum(end - start for start, end in spans) for day, spans in periods.items()})
            else:
                calendar.baseer_average_label = hhmm(int((Decimal(str(calendar.hours_per_day)) * 60).quantize(
                    Decimal('1'), rounding=ROUND_HALF_UP)))
            calendar.baseer_schedule_summary = '\n'.join('%s: %s' % (day.name, ' / '.join(
                '%s – %s%s' % (hhmm(start), hhmm(end if end <= 1440 else end - 1440), ' (+1)' if end > 1440 else '')
                for start, end in sorted(periods[day.weekday]))) for day in self.env['baseer.schedule.day'].search([]) if day.weekday in periods)

    def _get_days_per_week(self):
        if self.baseer_simple:
            return len(set(self._get_global_attendances().mapped('baseer_origin_day')))
        return super()._get_days_per_week()

    @api.model_create_multi
    def create(self, vals_list):
        revision_fields = ('baseer_superseded_by_id', 'baseer_effective_date', 'write_date', 'create_date')
        if not trusted(self) and any(any(key in vals or self.env.context.get('default_' + key) for key in revision_fields) for vals in vals_list):
            raise ValidationError(_('Schedule revision details are managed by Edit work schedule.'))
        if not trusted(self) and any(v.get('baseer_simple', self.env.context.get('default_baseer_simple', False)) for v in vals_list):
            raise ValidationError(_('Create this schedule through the simplified schedule form.'))
        return super().create(vals_list)

    def write(self, vals):
        if not trusted(self) and ({'baseer_superseded_by_id', 'baseer_effective_date', 'write_date', 'create_date'}.intersection(vals)
                or vals.get('baseer_is_template') and any(self.mapped('baseer_superseded_by_id'))):
            raise ValidationError(_('Schedule revision details are managed by Edit work schedule.'))
        protected = {'attendance_ids', 'attendance_ids_1st_week', 'attendance_ids_2nd_week', 'two_weeks_calendar',
                     'duration_based', 'schedule_type', 'flexible_hours', 'tz', 'company_id', 'hours_per_day', 'hours_per_week', 'baseer_simple'}
        if protected.intersection(vals) and not trusted(self) and (any(self.mapped('baseer_simple')) or vals.get('baseer_simple')):
            raise ValidationError(_('Use Set working times to create a new schedule. Existing employee schedules must remain unchanged.'))
        return super().write(vals)

    def action_baseer_edit_schedule(self):
        self.ensure_one()
        manager(self)
        self.check_access('write')
        self._baseer_check_editable()
        return self.env['baseer.schedule.wizard']._action({
            'default_edit_template': True, 'default_source_calendar_id': self.id,
            'default_employee_id': False, 'default_company_id': self.company_id.id,
            'default_name': self.name})

    def action_baseer_latest_schedule(self):
        self.ensure_one()
        self.check_access('read')
        calendar = self
        seen = set()
        while calendar.baseer_superseded_by_id and calendar.id not in seen:
            seen.add(calendar.id)
            calendar = calendar.baseer_superseded_by_id
            calendar.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': 'resource.calendar', 'res_id': calendar.id,
                'views': [(self.env.ref('baseer_work_schedule.view_baseer_schedule_template_form').id, 'form')],
                'view_mode': 'form', 'target': 'current'}

    def _baseer_check_editable(self):
        self.ensure_one()
        if not self.baseer_is_template or self.baseer_superseded_by_id or not self.active:
            raise ValidationError(_('This template was replaced or archived. Open the latest work schedule.'))
        if self.company_id != self.env.company:
            raise ValidationError(_('Choose a work schedule from the selected company.'))

    def _baseer_check_assignment_date(self, effective_date):
        for calendar in self:
            if calendar.baseer_effective_date and effective_date < calendar.baseer_effective_date:
                raise ValidationError(_('This work schedule is not yet effective. Choose its effective date or a later date.'))
            successor = calendar.baseer_superseded_by_id
            if successor and successor.baseer_effective_date and effective_date >= successor.baseer_effective_date:
                raise ValidationError(_('This work schedule has been replaced. Choose the latest work schedule.'))

    def _baseer_edit_candidates(self):
        self.ensure_one()
        candidates = self.env['hr.employee'].sudo().with_context(active_test=False).search([
            ('version_ids.resource_calendar_id', '=', self.id)], order='company_id, id', limit=201)
        if len(candidates) > 200:
            raise ValidationError(_('This template has more than 200 linked employees. Split the schedule change into smaller templates.'))
        return candidates

    def _baseer_template_revision(self, candidates=None):
        self.ensure_one()
        candidates = self._baseer_edit_candidates() if candidates is None else candidates
        values = (self.id, str(self.write_date), self.name, self.active, self.baseer_is_template,
                  self.baseer_superseded_by_id.id, str(self.baseer_effective_date), self.company_id.id, self.tz,
                  [(row.id, str(row.write_date)) for row in self.attendance_ids.sorted('id')],
                  [(row.id, str(row.write_date)) for row in self.global_leave_ids.sorted('id')],
                  [(employee.id, employee.active, employee.company_id.id, employee._baseer_schedule_revision())
                   for employee in candidates.sorted('id')])
        return hashlib.sha256(repr(values).encode()).hexdigest()

    def action_baseer_simple_schedule(self):
        self.ensure_one()
        manager(self)
        self.check_access('read')
        return self.env['baseer.schedule.wizard']._action({'default_source_calendar_id': self.id})

    def unlink(self):
        if self.filtered('baseer_simple') and self.env['hr.version'].sudo().with_context(active_test=False).search_count(
                [('resource_calendar_id', 'in', self.filtered('baseer_simple').ids)], limit=1):
            raise ValidationError(_('This schedule belongs to an employee history. Archive it instead of deleting it.'))
        return super(Calendar, self.with_context(_baseer_schedule_change=_TOKEN)).unlink()


class AttendanceLine(models.Model):
    _inherit = 'resource.calendar.attendance'
    baseer_origin_day = fields.Integer(copy=False, readonly=True)
    baseer_period_start = fields.Integer(copy=False, readonly=True)
    baseer_period_end = fields.Integer(copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not trusted(self):
            ids = [v.get('calendar_id', self.env.context.get('default_calendar_id')) for v in vals_list]
            if any(self.env['resource.calendar'].browse([i for i in ids if i]).mapped('baseer_simple')):
                raise ValidationError(_('Use the simplified form to change working times.'))
        return super().create(vals_list)

    def write(self, vals):
        if not trusted(self) and (any(self.calendar_id.mapped('baseer_simple')) or
                vals.get('calendar_id') and self.env['resource.calendar'].browse(vals['calendar_id']).baseer_simple):
            raise ValidationError(_('Use the simplified form to change working times.'))
        return super().write(vals)

    def unlink(self):
        if not trusted(self) and any(self.calendar_id.mapped('baseer_simple')):
            raise ValidationError(_('Use the simplified form to change working times.'))
        return super().unlink()


class Employee(models.Model):
    _inherit = 'hr.employee'

    def action_baseer_set_schedule(self):
        self.ensure_one()
        manager(self)
        self.check_access('write')
        return self.env['baseer.schedule.wizard']._action({'default_employee_id': self.id})

    def _baseer_schedule_revision(self):
        self.ensure_one()
        values = [(v.id, str(v.write_date), str(v.date_version), v.resource_calendar_id.id, v.active) for v in self.with_context(active_test=False).version_ids.sorted('id')]
        return hashlib.sha256(repr(values).encode()).hexdigest()

    def _baseer_version_for_period(self, first, last):
        # Permit schedule-only changes when every payroll input and result remains equal.
        self.ensure_one()
        versions = self.env['hr.version'].search([
            ('employee_id', '=', self.id), ('date_version', '<=', last), ('contract_date_start', '<=', last),
            '|', ('contract_date_end', '=', False), ('contract_date_end', '>=', first)], order='date_version desc')
        changes = versions.filtered(lambda v: first < v.date_version <= last)
        if len(versions) > 1 and changes and all(changes.mapped('baseer_schedule_version')):
            relevant = changes | versions.filtered(lambda v: v.date_version <= first)[:1]
            if len({v._baseer_schedule_payroll_signature() for v in relevant}) == 1:
                return versions[:1]
        return super()._baseer_version_for_period(first, last)


class Version(models.Model):
    _inherit = 'hr.version'
    baseer_schedule_version = fields.Boolean(copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if not trusted(self) and any(v.get('baseer_schedule_version', self.env.context.get('default_baseer_schedule_version', False)) for v in vals_list):
            raise ValidationError(_('Apply employee schedules through Set working times.'))
        if not trusted(self) and (any({'write_date', 'create_date'}.intersection(vals) for vals in vals_list)
                or any(self.env.context.get('default_' + key) for key in ('write_date', 'create_date'))):
            raise ValidationError(_('Employee history timestamps are managed automatically.'))
        lock_for_create(self, vals_list)
        # Native defaults/computes may choose the calendar during create. Employees
        # are already locked; touch the resulting calendars before returning.
        records = super().create(vals_list)
        touch_calendars(records.resource_calendar_id)
        for record in records:
            record.resource_calendar_id._baseer_check_assignment_date(record.date_version)
        return records

    def write(self, vals):
        if not trusted(self) and {'baseer_schedule_version', 'write_date', 'create_date'}.intersection(vals):
            raise ValidationError(_('Apply employee schedules through Set working times.'))
        membership = {'employee_id', 'resource_calendar_id', 'date_version', 'active'}.intersection(vals)
        if vals:
            self.check_access('write')
            employees = self.employee_id | self.env['hr.employee'].browse(vals.get('employee_id'))
            lock_employees(employees)
        if membership:
            calendars = self.resource_calendar_id | self.env['resource.calendar'].browse(vals.get('resource_calendar_id'))
            touch_calendars(calendars)
        result = super().write(vals)
        if membership:
            touch_calendars(calendars | self.resource_calendar_id)
            for record in self:
                if membership != {'active'} or record.active:
                    record.resource_calendar_id._baseer_check_assignment_date(record.date_version)
        return result

    def unlink(self):
        self.check_access('unlink')
        lock_employees(self.employee_id)
        calendars = self.resource_calendar_id
        touch_calendars(calendars)
        return super().unlink()

    def _baseer_schedule_payroll_signature(self, calendar=None):
        self.ensure_one()
        from odoo.addons.baseer_payroll.models.common import split_salary
        # This helper is only called through authorized payroll paths or scoped preflight.
        cal = calendar or self.resource_calendar_id
        result = split_salary(self.wage, self.baseer_allowance_total, self.baseer_salary_mode,
                              cal.hours_per_day, self.baseer_work_days)
        return (self.wage, self.baseer_allowance_total, self.baseer_salary_mode, self.baseer_work_days,
                self.contract_date_start, self.contract_date_end, tuple(result))

    def _get_leaves_from_vals(self, vals):
        if trusted(self):
            return self.env['hr.leave']  # Explicit preflight already blocked the affected interval.
        return super()._get_leaves_from_vals(vals)

    def _get_leaves(self, extra_domain=None):
        if trusted(self):
            return self.env['hr.leave']
        return super()._get_leaves(extra_domain)


class Payslip(models.Model):
    _inherit = 'hr.payslip'

    @api.model
    def get_worked_day_lines(self, versions, date_from, date_to):
        result = []
        for version in versions:
            start = max(fields.Date.to_date(date_from), version.date_start or version.date_version)
            stop = min(fields.Date.to_date(date_to), version.date_end or fields.Date.to_date(date_to))
            if start <= stop:
                result.extend(super().get_worked_day_lines(version.with_context(version_id=version.id), start, stop))
        return result


class ActualAttendance(models.Model):
    _inherit = 'hr.attendance'

    @api.model_create_multi
    def create(self, vals_list):
        lock_for_create(self, vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self.check_access('write')
        if {'employee_id', 'check_in', 'check_out'}.intersection(vals):
            employees = self.employee_id | self.env['hr.employee'].browse(vals.get('employee_id'))
            lock_employees(employees)
        return super().write(vals)


class TimeOff(models.Model):
    _inherit = 'hr.leave'

    @api.model_create_multi
    def create(self, vals_list):
        lock_for_create(self, vals_list)
        return super().create(vals_list)

    def write(self, vals):
        self.check_access('write')
        if {'employee_id', 'date_from', 'date_to', 'request_date_from', 'request_date_to', 'state', 'resource_calendar_id'}.intersection(vals):
            employees = self.employee_id | self.env['hr.employee'].browse(vals.get('employee_id'))
            lock_employees(employees)
        return super().write(vals)


class Period(models.TransientModel):
    _name = 'baseer.schedule.period'
    _description = 'Work Schedule Period'
    _order = 'id'
    wizard_id = fields.Many2one('baseer.schedule.wizard', required=True, ondelete='cascade')
    time_from = fields.Char(string='From', default='08:00', required=True)
    time_to = fields.Char(string='To', default='16:00', required=True)
    custom_days = fields.Boolean(string='Different days')
    day_ids = fields.Many2many('baseer.schedule.day', string='Days for this period')
    duration_label = fields.Char(compute='_compute_duration', string='Period hours')

    @api.depends('time_from', 'time_to')
    def _compute_duration(self):
        for line in self:
            try:
                start, end = minutes(line.time_from), minutes(line.time_to, end=True)
                line.duration_label = hhmm(end - start if end >= start else end + 1440 - start) + (' (+1)' if end < start else '')
            except ValueError:
                line.duration_label = False


class Wizard(models.TransientModel):
    _name = 'baseer.schedule.wizard'
    _description = 'Set Working Times'
    name = fields.Char(string='Schedule name', required=True, default=lambda self: _('Work schedule'))
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    employee_id = fields.Many2one('hr.employee', string='Employee')
    source_calendar_id = fields.Many2one('resource.calendar', string='Use an existing schedule')
    effective_date = fields.Date(string='Applies from', required=True, default=fields.Date.context_today)
    day_ids = fields.Many2many('baseer.schedule.day', string='Working days', default=lambda self: self.env['baseer.schedule.day'].search([('weekday', '!=', 4)]))
    period_ids = fields.One2many('baseer.schedule.period', 'wizard_id', string='Working periods')
    total_label = fields.Char(compute='_compute_totals', string='Weekly hours')
    average_label = fields.Char(compute='_compute_totals', string='Average working day (HH:MM)')
    daily_summary = fields.Text(compute='_compute_totals', string='Daily hours')
    validation_message = fields.Char(compute='_compute_totals')
    source_warning = fields.Char(readonly=True)
    time_zone = fields.Char(compute='_compute_time_zone', string='Time zone')
    save_as_template = fields.Boolean(string='Also save as a template')
    template_name = fields.Char(string='Template name')
    expected_revision = fields.Char()
    applied_calendar_id = fields.Many2one('resource.calendar', readonly=True)
    edit_template = fields.Boolean(string='Edit work schedule')
    template_revision = fields.Char(readonly=True)
    affected_employee_ids = fields.Many2many('hr.employee', compute='_compute_affected_employees', string='Employees to update')
    affected_count = fields.Integer(compute='_compute_affected_employees', string='Employees to update')

    def _affected_employees(self, candidates):
        self.ensure_one()
        return candidates.filtered(lambda employee: employee.active and
            employee._get_version(self.effective_date).resource_calendar_id == self.source_calendar_id)

    @api.depends('edit_template', 'source_calendar_id', 'effective_date')
    def _compute_affected_employees(self):
        for wizard in self:
            employees = self.env['hr.employee']
            if wizard.edit_template and wizard.source_calendar_id and wizard.effective_date:
                wizard.source_calendar_id.check_access('read')
                candidates = wizard.source_calendar_id._baseer_edit_candidates()
                employee_ids = wizard._affected_employees(candidates).ids
                employees = self.env['hr.employee'].with_context(active_test=False).browse(employee_ids)
                employees.check_access('read')
            wizard.affected_employee_ids = employees
            wizard.affected_count = len(employees)

    @api.depends('source_calendar_id', 'employee_id', 'company_id', 'effective_date')
    def _compute_time_zone(self):
        for wizard in self:
            current = wizard.employee_id._get_version(wizard.effective_date).resource_calendar_id if wizard.employee_id and wizard.effective_date else self.env['resource.calendar']
            wizard.time_zone = wizard.source_calendar_id.tz or current.tz or wizard.company_id.resource_calendar_id.tz or 'Asia/Riyadh'

    @api.model
    def _action(self, context=None):
        return {'type': 'ir.actions.act_window', 'name': _('Set working times'), 'res_model': self._name,
                'view_mode': 'form', 'target': 'new', 'context': dict(self.env.context, **(context or {}))}

    @api.model
    def default_get(self, field_names):
        vals = super().default_get(field_names)
        if vals.get('edit_template', self.env.context.get('default_edit_template')):
            source = self.env['resource.calendar'].browse(vals.get('source_calendar_id', self.env.context.get('default_source_calendar_id')))
            manager(self)
            if not source:
                raise ValidationError(_('Choose the template to edit.'))
            source.check_access('write')
            source._baseer_check_editable()
            defaults = dict(self._source_values(source), source_calendar_id=source.id, edit_template=True,
                            company_id=source.company_id.id, employee_id=False, name=source.name,
                            template_revision=source._baseer_template_revision())
            vals.update({key: value for key, value in defaults.items() if key in field_names})
        employee = self.env['hr.employee'].browse(vals.get('employee_id'))
        if employee:
            employee.check_access('read')
            vals.update(company_id=employee.company_id.id, expected_revision=employee._baseer_schedule_revision())
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('edit_template', self.env.context.get('default_edit_template')):
                source_id = vals.get('source_calendar_id', self.env.context.get('default_source_calendar_id'))
                names = ['edit_template', 'source_calendar_id', 'company_id', 'employee_id', 'name',
                         'template_revision', 'day_ids', 'period_ids', 'source_warning']
                defaults = self.with_context(default_edit_template=True, default_source_calendar_id=source_id).default_get(names)
                for key, value in defaults.items():
                    vals.setdefault(key, value)
                if vals.get('employee_id') or vals.get('save_as_template') or vals.get('template_name'):
                    raise ValidationError(_('Template editing updates its linked employees. Open a separate form to copy a schedule.'))
                if vals['name'] != defaults['name'] or vals['company_id'] != defaults['company_id']:
                    raise ValidationError(_('Keep the template name and company unchanged when editing its working times.'))
            if vals.get('employee_id') and not vals.get('expected_revision'):
                emp = self.env['hr.employee'].browse(vals['employee_id'])
                emp.check_access('read')
                vals['expected_revision'] = emp._baseer_schedule_revision()
            if vals.get('applied_calendar_id', self.env.context.get('default_applied_calendar_id')):
                raise ValidationError(_('Create a new schedule form.'))
        return super().create(vals_list)

    def write(self, vals):
        if 'applied_calendar_id' in vals and not trusted(self):
            raise ValidationError(_('Create a new schedule form.'))
        if not trusted(self):
            if 'edit_template' in vals and any(wizard.edit_template != vals['edit_template'] for wizard in self):
                raise ValidationError(_('Open a new form to change the schedule operation.'))
            protected = {'source_calendar_id', 'employee_id', 'company_id', 'name', 'template_revision', 'save_as_template', 'template_name'}
            for wizard in self.filtered('edit_template'):
                for key in protected.intersection(vals):
                    current = wizard[key].id if self._fields[key].type == 'many2one' else wizard[key]
                    if (current or False) != (vals[key] or False):
                        raise ValidationError(_('Keep the template and employee scope unchanged. Reopen Edit work schedule.'))
        return super().write(vals)

    @api.onchange('employee_id')
    def _onchange_employee_date(self):
        if self.employee_id:
            self.company_id = self.employee_id.company_id
            self.expected_revision = self.employee_id._baseer_schedule_revision()

    @api.onchange('source_calendar_id')
    def _onchange_source_calendar(self):
        source = self.source_calendar_id
        if not source:
            self.source_warning = False
            return
        self.update(self._source_values(source))

    @api.model
    def _source_values(self, source):
        source.check_access('read')
        values = {'name': source.name, 'period_ids': [Command.clear()], 'source_warning': False}
        if source.duration_based or source.two_weeks_calendar or source.schedule_type != 'fully_fixed':
            values['source_warning'] = _('This schedule has no simple weekly clock times. Enter the actual working times to create a new schedule.')
            return values
        mapping = defaultdict(set)
        for row in source._get_global_attendances():
            if source.baseer_simple:
                mapping[row.baseer_period_start, row.baseer_period_end].add(row.baseer_origin_day)
            else:
                start, end = round(row.hour_from * 60), round(row.hour_to * 60)
                if abs(row.hour_from * 60 - start) > 0.00001 or abs(row.hour_to * 60 - end) > 0.00001:
                    values['source_warning'] = _('Enter working times with whole-minute precision.')
                    return values
                mapping[start, end].add(int(row.dayofweek))
        all_days = set().union(*mapping.values()) if mapping else set()
        values['day_ids'] = [Command.set(self.env['baseer.schedule.day'].search([('weekday', 'in', sorted(all_days))]).ids)]
        values['period_ids'] = [Command.clear()] + [Command.create({'time_from': hhmm(start), 'time_to': hhmm(end if end <= 1440 else end - 1440),
            'custom_days': days != all_days, 'day_ids': [Command.set(self.env['baseer.schedule.day'].search([('weekday', 'in', sorted(days))]).ids)]})
            for (start, end), days in sorted(mapping.items())]
        return values

    def _compiled(self):
        self.ensure_one()
        if len(self.period_ids) > 28:
            raise ValidationError(_('Use no more than 28 working periods.'))
        try:
            return compile_periods([(line.day_ids.mapped('weekday') if line.custom_days else self.day_ids.mapped('weekday'),
                minutes(line.time_from), minutes(line.time_to, end=True)) for line in self.period_ids])
        except ValueError as error:
            if str(error) == 'overlap':
                raise ValidationError(_('Working periods overlap, including periods after midnight.')) from error
            raise ValidationError(_('Select working days and enter valid, non-zero periods in HH:MM format.')) from error

    @api.depends('day_ids', 'period_ids.time_from', 'period_ids.time_to', 'period_ids.custom_days', 'period_ids.day_ids')
    def _compute_totals(self):
        for wizard in self:
            try:
                _, totals = wizard._compiled()
                wizard.total_label = hhmm(sum(totals.values()))
                wizard.average_label = daily_average_label(totals)
                wizard.daily_summary = '\n'.join('%s: %s' % (day.name, hhmm(totals[day.weekday]))
                    for day in self.env['baseer.schedule.day'].search([]) if day.weekday in totals)
                wizard.validation_message = False
            except ValidationError as error:
                wizard.total_label = wizard.average_label = '—'
                wizard.daily_summary = False
                wizard.validation_message = str(error)

    def _create_calendar(self, name, is_template):
        fragments, totals = self._compiled()
        zone = self.time_zone
        rows = [Command.clear()]
        for day, start, end, origin, original_start, original_end in fragments:
            rows.append(Command.create({'name': '%s – %s' % (hhmm(start), hhmm(end)), 'dayofweek': str(day),
                'hour_from': float(Decimal(start) / 60), 'hour_to': float(Decimal(end) / 60),
                'day_period': 'morning' if start < 720 else 'afternoon',
                'duration_days': float(Decimal(end - start) / Decimal(totals[origin])),
                'baseer_origin_day': origin, 'baseer_period_start': original_start, 'baseer_period_end': original_end,
                'sequence': day * 1440 + start}))
        vals = {
            'name': name, 'company_id': self.company_id.id, 'tz': zone, 'schedule_type': 'fully_fixed',
            'duration_based': False, 'two_weeks_calendar': False, 'baseer_simple': True,
            'baseer_is_template': is_template, 'attendance_ids': rows}
        source = self.source_calendar_id or (self.employee_id._get_version(self.effective_date).resource_calendar_id if self.employee_id else self.company_id.resource_calendar_id)
        # HR schedule managers may read holidays without permission to author them.
        # Create the calendar under native caller ACLs, then copy only the readable
        # global holiday values onto that newly created calendar under scoped elevation.
        holidays = [leave._copy_leave_vals() for leave in source.global_leave_ids.filtered(lambda leave: not leave.resource_id)]
        vals['global_leave_ids'] = [Command.clear()]
        context = {key: value for key, value in self.env.context.items() if not key.startswith('default_')}
        calendar = self.env['resource.calendar'].with_context(context, _baseer_schedule_change=_TOKEN).create(vals)
        if holidays:
            calendar.sudo().with_context(_baseer_schedule_change=_TOKEN).write({
                'global_leave_ids': [Command.create(value) for value in holidays]})
        return calendar

    def _employee_preflight(self, employee=None, expected_revision=None, already_locked=False):
        employee = self.employee_id if employee is None else employee
        employee.check_access('write')
        if not already_locked:
            lock_employees(employee)
        employee.version_ids.invalidate_recordset()
        expected_revision = self.expected_revision if expected_revision is None else expected_revision
        if expected_revision != employee._baseer_schedule_revision():
            raise ValidationError(_('The employee schedule changed while this form was open. Reopen Set working times.'))
        if self.effective_date < fields.Date.context_today(self):
            raise ValidationError(_('Choose today or a future date to preserve past attendance and payroll.'))
        existing = employee.version_ids.filtered(lambda v: v.date_version == self.effective_date)
        initial = len(employee.version_ids) == 1 and existing
        if initial:
            # The same-date shortcut is initial setup only, never a historical rewrite.
            for model in ('hr.attendance', 'hr.leave', 'hr.payslip', 'hr.leave.allocation'):
                if self.env[model].sudo().with_context(active_test=False).search_count([('employee_id', '=', employee.id)], limit=1):
                    raise ValidationError(_('This employee already has attendance, leave or payroll history. Apply a new schedule from another date.'))
        if existing and not initial:
            raise ValidationError(_('An employee version already starts on this date. Choose another date.'))
        next_versions = employee.version_ids.filtered(lambda v: v.date_version > self.effective_date).sorted('date_version')
        end_date = next_versions[:1].date_version if next_versions else None
        calendar = employee._get_version(self.effective_date).resource_calendar_id
        if self.source_calendar_id and self.source_calendar_id.tz != calendar.tz:
            raise ValidationError(_('Choose a template in the employee work schedule time zone.'))
        tz = pytz.timezone(calendar.tz or 'Asia/Riyadh')
        start = tz.localize(datetime.combine(self.effective_date, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        stop = tz.localize(datetime.combine(end_date, time.min)).astimezone(pytz.utc).replace(tzinfo=None) if end_date else datetime.max
        # Narrow read-only existence checks avoid exposing payroll/leave records to HR.
        common = [('employee_id', '=', employee.id)]
        if self.env['hr.attendance'].sudo().search_count(common + [('check_in', '<', stop), '|', ('check_out', '=', False), ('check_out', '>', start)], limit=1):
            raise ValidationError(_('Attendance exists in the affected period. Choose a later effective date.'))
        if self.env['hr.leave'].sudo().search_count(common + [('state', 'not in', ['refuse', 'cancel']), ('date_from', '<', stop), ('date_to', '>', start)], limit=1):
            raise ValidationError(_('Time off exists in the affected period. Review it before changing the schedule.'))
        if self.env['hr.payslip'].sudo().search_count(common + [('state', 'not in', ['draft', 'cancel']), ('date_to', '>=', self.effective_date)] + ([('date_from', '<', end_date)] if end_date else []), limit=1):
            raise ValidationError(_('An approved payslip covers the affected period. Choose a later effective date.'))
        return existing if initial else self.env['hr.version']

    def _apply_template_edit(self):
        source = self.source_calendar_id
        if self.employee_id or self.save_as_template or self.template_name or not source:
            raise ValidationError(_('Template editing updates its linked employees. Open a separate form to copy a schedule.'))
        source.check_access('write')
        source._baseer_check_editable()
        if self.name != source.name or self.company_id != source.company_id:
            raise ValidationError(_('Keep the template name and company unchanged when editing its working times.'))
        if self.effective_date < fields.Date.context_today(self):
            raise ValidationError(_('Choose today or a future date to preserve past attendance and payroll.'))
        source._baseer_check_assignment_date(self.effective_date)
        candidates = source._baseer_edit_candidates()
        lock_employees(candidates)
        touch_calendars(source)
        candidates.invalidate_recordset()
        candidates.with_context(active_test=False).version_ids.invalidate_recordset()
        current_candidates = source._baseer_edit_candidates()
        if set(current_candidates.ids) != set(candidates.ids):
            raise ValidationError(_('Linked employees changed while this form was open. Reopen Edit work schedule.'))
        source._baseer_check_editable()
        if self.template_revision != source._baseer_template_revision(candidates):
            raise ValidationError(_('The template or linked employees changed while this form was open. Reopen Edit work schedule.'))
        affected = self.env['hr.employee'].browse(self._affected_employees(candidates).ids)
        affected.check_access('write')
        initials = {}
        for employee in affected:
            try:
                if employee.company_id != self.company_id:
                    raise ValidationError(_('Choose a work schedule from the same company.'))
                if employee.with_context(active_test=False).version_ids.filtered(lambda version: version.date_version > self.effective_date):
                    raise ValidationError(_('A future employee version already exists. Review that planned change before editing this template.'))
                initials[employee.id] = self._employee_preflight(employee, employee._baseer_schedule_revision(), already_locked=True)
            except (ValidationError, AccessError) as error:
                raise ValidationError(_('%(employee)s: %(reason)s', employee=employee.name, reason=str(error))) from error
        calendar = self._create_calendar(source.name, True)
        calendar.with_context(_baseer_schedule_change=_TOKEN).write({'baseer_effective_date': self.effective_date})
        # Finish every preflight before writing even the first employee version.
        for employee in affected:
            old = employee._get_version(self.effective_date)
            if not initials[employee.id] and self.effective_date.day != 1 and old.sudo()._baseer_schedule_payroll_signature() != old.sudo()._baseer_schedule_payroll_signature(calendar):
                raise ValidationError(_('%(employee)s: These hours change the salary breakdown. Apply them from the first day of a month.', employee=employee.name))
        for employee in affected:
            initial = initials[employee.id]
            if initial:
                initial.with_context(_baseer_schedule_change=_TOKEN).write({'resource_calendar_id': calendar.id})
            else:
                employee.with_context(_baseer_schedule_change=_TOKEN).create_version({
                    'date_version': self.effective_date, 'resource_calendar_id': calendar.id, 'baseer_schedule_version': True})
        source.with_context(_baseer_schedule_change=_TOKEN).write({
            'baseer_is_template': False, 'baseer_superseded_by_id': calendar.id})
        self.with_context(_baseer_schedule_change=_TOKEN).write({'applied_calendar_id': calendar.id})
        return calendar.action_baseer_latest_schedule()

    def action_apply(self):
        self.ensure_one()
        manager(self)
        self.check_access('write')
        self.env.cr.execute('SELECT id FROM baseer_schedule_wizard WHERE id=%s FOR UPDATE', [self.id])
        self.invalidate_recordset(['applied_calendar_id'])
        if self.applied_calendar_id:
            return {'type': 'ir.actions.act_window_close'}
        if self.company_id != self.env.company or self.employee_id and self.employee_id.company_id != self.company_id:
            raise ValidationError(_('Use the employee company selected in the interface.'))
        if self.source_calendar_id:
            self.source_calendar_id.check_access('read')
            if self.source_calendar_id.company_id and self.source_calendar_id.company_id != self.company_id:
                raise ValidationError(_('Choose a work schedule from the same company.'))
        self._compiled()
        with self.env.cr.savepoint():
            if self.edit_template:
                return self._apply_template_edit()
            if self.employee_id and self.source_calendar_id:
                self.source_calendar_id._baseer_check_assignment_date(self.effective_date)
            initial = self._employee_preflight() if self.employee_id else self.env['hr.version']
            calendar = self._create_calendar(self.name, not bool(self.employee_id))
            if self.employee_id:
                old = self.employee_id._get_version(self.effective_date)
                if not initial and self.effective_date.day != 1 and old.sudo()._baseer_schedule_payroll_signature() != old.sudo()._baseer_schedule_payroll_signature(calendar):
                    raise ValidationError(_('These hours change the salary breakdown. Apply them from the first day of a month.'))
                values = {'date_version': self.effective_date, 'resource_calendar_id': calendar.id, 'baseer_schedule_version': True}
                if initial:
                    initial.with_context(_baseer_schedule_change=_TOKEN).write({'resource_calendar_id': calendar.id})
                else:
                    self.employee_id.with_context(_baseer_schedule_change=_TOKEN).create_version(values)
                if self.save_as_template:
                    if not (self.template_name or '').strip():
                        raise ValidationError(_('Enter a name for the template.'))
                    self._create_calendar(self.template_name, True)
            self.with_context(_baseer_schedule_change=_TOKEN).write({'applied_calendar_id': calendar.id})
        if self.employee_id:
            return {'type': 'ir.actions.client', 'tag': 'soft_reload'}
        return {'type': 'ir.actions.act_window', 'res_model': 'resource.calendar', 'res_id': calendar.id,
                'views': [(self.env.ref('baseer_work_schedule.view_baseer_schedule_template_form').id, 'form')],
                'view_mode': 'form', 'target': 'current'}
