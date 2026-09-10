from odoo import api, models, Command


# Explicit inventory: installing another application requires reviewing its administrator group.
OWNER_GROUP_XMLIDS = (
    'base.group_system', 'base.group_erp_manager', 'base.group_partner_manager',
    'account.group_account_manager', 'eh_account_base.group_eh_manager',
    'purchase.group_purchase_manager', 'sales_team.group_sale_manager',
    'point_of_sale.group_pos_manager', 'stock.group_stock_manager',
    'product.group_product_manager', 'project.group_project_manager',
    'maintenance.group_equipment_manager', 'hr.group_hr_manager',
    'hr_attendance.group_hr_attendance_manager', 'hr_holidays.group_hr_holidays_manager',
    'hr_expense.group_hr_expense_manager', 'hr_contract.group_hr_contract_manager',
    'om_hr_payroll.group_hr_payroll_manager',
    'hr_payroll_community.group_hr_payroll_community_manager',
    'spreadsheet_dashboard.group_dashboard_manager', 'website.group_website_designer',
)

DENIED_MODELS = (
    'crm.lead', 'project.project', 'project.task',
    'maintenance.request', 'maintenance.equipment', 'hr.employee', 'hr.contract',
    'hr.payslip', 'hr.payslip.run', 'hr.payslip.line', 'hr.payslip.input',
    'hr.payslip.worked_days', 'hr.leave', 'hr.leave.allocation', 'hr.attendance',
    'hr.expense', 'baseer.hr.service', 'baseer.hr.eos', 'baseer.hr.loan',
    'baseer.hr.loan.line', 'baseer.hr.loan.allocation', 'baseer.payroll.correction',
    'baseer.payroll.settlement', 'baseer.payroll.settlement.line',
)


class AccessRoleSeed(models.AbstractModel):
    _name = 'baseer.access.role.seed'
    _description = 'Baseer Access Role Seed'

    @api.model
    def _setup_roles(self):
        self.env['res.users']._baseer_require_role_admin()
        groups = self.env['res.groups']
        for xmlid in OWNER_GROUP_XMLIDS:
            groups |= self.env.ref(xmlid, raise_if_not_found=False) or self.env['res.groups']
        self.env.ref('baseer_access_roles.group_owner').write({'implied_ids': [Command.set(groups.ids)]})
        domain = "[(0, '=', 1)] if user.has_group('baseer_access_roles.group_cashier') or user.has_group('baseer_access_roles.group_accountant') else [(1, '=', 1)]"
        for model_name in DENIED_MODELS:
            if model_name not in self.env:
                continue
            xmlid = 'deny_' + model_name.replace('.', '_')
            values = {
                'name': 'Baseer limited roles: ' + model_name,
                'model_id': self.env['ir.model']._get_id(model_name),
                'domain_force': domain, 'groups': [Command.clear()],
                'perm_read': True, 'perm_write': True, 'perm_create': True, 'perm_unlink': True,
            }
            existing = self.env.ref('baseer_access_roles.' + xmlid, raise_if_not_found=False)
            if existing:
                existing.write(values)
            else:
                rule = self.env['ir.rule'].create(values)
                self.env['ir.model.data'].create({
                    'module': 'baseer_access_roles', 'name': xmlid,
                    'model': 'ir.rule', 'res_id': rule.id, 'noupdate': True,
                })
        return True


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        owners = self.env['res.users'].sudo().with_context(active_test=False).search([
            ('baseer_access_role', '=', 'owner'),
        ])
        if owners:
            owners.write({'company_ids': [Command.link(company.id) for company in companies]})
        return companies
