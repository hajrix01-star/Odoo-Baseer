from odoo import api, fields, models, _


class Employee(models.Model):
    _inherit = 'hr.employee'

    baseer_service_ids = fields.One2many('baseer.hr.service', 'employee_id', string='Employee Services', groups='hr.group_hr_user')
    baseer_service_count = fields.Integer(compute='_compute_service_count', groups='hr.group_hr_user')

    def _compute_service_count(self):
        counts = dict(self.env['baseer.hr.service'].with_context(active_test=False)._read_group(
            [('employee_id', 'in', self.ids)], ['employee_id'], ['__count']))
        for employee in self:
            employee.baseer_service_count = counts.get(employee, 0)

    def action_view_services(self):
        self.ensure_one()
        self.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Employee Services'),
                'res_model': 'baseer.hr.service', 'view_mode': 'list,kanban,form',
                'domain': [('employee_id', '=', self.id), ('company_id', '=', self.company_id.id)],
                'context': {'default_employee_id': self.id, 'active_test': False}}
