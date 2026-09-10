"""Keep private HR accounting outside the limited operational roles."""
from odoo import api, fields, models, Command
from odoo.fields import Domain
from odoo.exceptions import AccessError


def limited(env):
    return (env.user.has_group('baseer_access_roles.group_cashier')
            or env.user.has_group('baseer_access_roles.group_accountant'))


def private_accounts(env):
    companies = env.user.company_ids.sudo()
    accounts = env['account.account'].browse()
    for name in ('baseer_salary_expense_id', 'baseer_salary_payable_id',
                 'baseer_deduction_account_id', 'baseer_loan_account_id'):
        if name in companies._fields:
            accounts |= companies.mapped(name)
    return accounts.ids


class PrivacyUsers(models.Model):
    _inherit = 'res.users'

    def _baseer_public_move_domain(self, prefix=''):
        """Private rule helper; fixed prefixes are supplied by seeded rules only."""
        if not limited(self.env):
            return Domain.TRUE
        move = self.env['account.move']
        private = [Domain('line_ids', 'any!', Domain('account_id', 'in', private_accounts(self.env)))]
        for name in ('baseer_payslip_id', 'baseer_correction_payslip_id',
                     'baseer_correction_kind', 'baseer_eos_id', 'baseer_hr_service_id',
                     'baseer_loan_id'):
            if name in move._fields:
                private.append(Domain(name, '!=', False))
        private.append(Domain('origin_payment_id', 'any!', Domain('baseer_private_hr', '=', True)))
        domain = Domain.OR(private)
        for relation in reversed(prefix.rstrip('.').split('.')) if prefix else ():
            domain = Domain(relation, 'any!', domain)
        # Negative existence allows an empty journal head during native bill creation.
        return ~domain

    def _baseer_public_payment_domain(self):
        if not limited(self.env):
            return Domain.TRUE
        return Domain([('baseer_private_hr', '=', False),
                       ('destination_account_id', 'not in', private_accounts(self.env))]) & self._baseer_public_move_domain('move_id.')

    def _baseer_public_statement_domain(self, model_name):
        if not limited(self.env):
            return Domain.TRUE
        model = self.env[model_name]
        domain = Domain('pos_session_id', '!=', False) if 'pos_session_id' in model._fields else Domain.FALSE
        if model_name == 'account.bank.statement.line':
            domain &= self._baseer_public_move_domain('move_id.')
        return domain


class PrivacyPayment(models.Model):
    _inherit = 'account.payment'

    # A permanent classification preserves privacy if payroll configuration changes.
    baseer_private_hr = fields.Boolean(readonly=True, copy=False, groups='base.group_system')

    def _baseer_classify_private_payment(self):
        for payment in self.sudo():
            company = payment.company_id
            accounts = self.env['account.account'].browse()
            for name in ('baseer_salary_expense_id', 'baseer_salary_payable_id',
                         'baseer_deduction_account_id', 'baseer_loan_account_id'):
                if name in company._fields:
                    accounts |= company[name]
            if payment.baseer_private_hr or payment.destination_account_id in accounts:
                if not self.env.su and limited(self.env):
                    raise AccessError(self.env._('Salary payments are outside your access role.'))
                if not payment.baseer_private_hr:
                    super(PrivacyPayment, payment).write({'baseer_private_hr': True})

    @api.model_create_multi
    def create(self, vals_list):
        if any('baseer_private_hr' in vals for vals in vals_list) or 'default_baseer_private_hr' in self.env.context:
            raise AccessError(self.env._('HR payment classification is managed by the system.'))
        with self.env.cr.savepoint():
            payments = super().create(vals_list)
            payments._baseer_classify_private_payment()
        return payments

    def write(self, vals):
        if 'baseer_private_hr' in vals:
            raise AccessError(self.env._('HR payment classification is managed by the system.'))
        with self.env.cr.savepoint():
            result = super().write(vals)
            self._baseer_classify_private_payment()
        return result


class PrivacySeed(models.AbstractModel):
    _inherit = 'baseer.access.role.seed'

    @api.model
    def _setup_roles(self):
        result = super()._setup_roles()
        rules = {
            'account.move': "user._baseer_public_move_domain()",
            'account.move.line': "user._baseer_public_move_domain('move_id.')",
            'account.payment': "user._baseer_public_payment_domain()",
            'account.invoice.report': "user._baseer_public_move_domain('move_id.')",
            'account.analytic.line': "user._baseer_public_move_domain('move_line_id.move_id.')",
            'account.partial.reconcile': "user._baseer_public_move_domain('debit_move_id.move_id.') & user._baseer_public_move_domain('credit_move_id.move_id.')",
            'account.full.reconcile': "user._baseer_public_move_domain('reconciled_line_ids.move_id.')",
        }
        # Bank reconciliation is outside these roles. Keep native POS statement support.
        for model_name in ('account.bank.statement', 'account.bank.statement.line'):
            if model_name not in self.env:
                continue
            rules[model_name] = "user._baseer_public_statement_domain('" + model_name + "')"
        for model_name, domain in rules.items():
            if model_name not in self.env:
                continue
            xmlid = 'private_hr_' + model_name.replace('.', '_')
            values = {'name': 'Baseer payroll privacy: ' + model_name,
                      'model_id': self.env['ir.model']._get_id(model_name),
                      'domain_force': domain, 'groups': [Command.clear()]}
            rule = self.env.ref('baseer_access_roles.' + xmlid, raise_if_not_found=False)
            if rule:
                rule.write(values)
            else:
                rule = self.env['ir.rule'].create(values)
                self.env['ir.model.data'].create({'module': 'baseer_access_roles', 'name': xmlid,
                                                'model': 'ir.rule', 'res_id': rule.id, 'noupdate': True})
        return result


class PrivacyCompany(models.Model):
    _inherit = 'res.company'

    def write(self, vals):
        result = super().write(vals)
        if {'baseer_salary_expense_id', 'baseer_salary_payable_id',
            'baseer_deduction_account_id', 'baseer_loan_account_id'} & vals.keys():
            # Rule domains embed this reviewed account inventory.
            self.env.registry.clear_cache()
        return result


class PrivacyJournal(models.Model):
    _inherit = 'account.journal'

    @api.model
    def _has_field_access(self, field, operation):
        if (not self.env.su and limited(self.env) and field.name in {
            'kanban_dashboard', 'kanban_dashboard_graph', 'current_statement_balance',
            'has_statement_lines', 'last_statement_id',
        }):
            return False
        return super()._has_field_access(field, operation)


class PrivacyAccount(models.Model):
    _inherit = 'account.account'

    @api.model
    def _has_field_access(self, field, operation):
        if (not self.env.su and limited(self.env)
                and field.name in {'opening_debit', 'opening_credit', 'opening_balance'}):
            return False
        return super()._has_field_access(field, operation)
