"""Optional company-owned working schedules, using native duration calendars."""
from odoo import api, Command, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import clean_context


class Company(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        # Native company creation has already enforced permissions.
        companies.sudo()._baseer_seed_work_schedules()
        return companies

    @api.model
    def _baseer_init_work_schedules(self):
        self.with_context(active_test=False).search([])._baseer_seed_work_schedules()

    def _baseer_seed_work_schedules(self):
        companies = self.with_context(clean_context(self.env.context))
        for company in companies.sorted('id'):
            self.env.cr.execute('SELECT id FROM res_company WHERE id = %s FOR UPDATE', [company.id])
            data = company.env['ir.model.data']
            for weekly, days, duration in ((84, tuple(range(7)), 12), (60, (0, 1, 2, 3, 5, 6), 10)):
                key = f'work_schedule_{weekly}_company_{company.id}'
                identity = data.search([('module', '=', 'baseer_payroll'), ('name', '=', key)], limit=1)
                if identity:
                    if identity.model != 'resource.calendar':
                        raise ValidationError(_('The working schedule seed reference is invalid.'))
                    calendar = company.env['resource.calendar'].browse(identity.res_id).exists()
                    if not calendar or calendar.company_id != company:
                        raise ValidationError(_('The working schedule seed reference is invalid.'))
                    continue  # Preserve administrator edits and archived schedules.
                calendar = company.env['resource.calendar'].create({
                    'name': f'{weekly} h/week · {len(days)} days | {weekly} ساعة أسبوعيًا · {len(days)} أيام',
                    'company_id': company.id,
                    'tz': company.resource_calendar_id.tz or 'Asia/Riyadh',
                    'schedule_type': 'fully_fixed',
                    'duration_based': True,
                    'two_weeks_calendar': False,
                    'attendance_ids': [Command.clear()] + [Command.create({
                        'name': f'{duration} h | {duration} ساعة',
                        'dayofweek': str(day),
                        'day_period': 'full_day',
                        'duration_hours': duration,
                        'sequence': day,
                    }) for day in days],
                })
                data.create({'module': 'baseer_payroll', 'name': key,
                             'model': 'resource.calendar', 'res_id': calendar.id, 'noupdate': True})
