"""Fill-only native accounting defaults; never replace an established chart."""
from odoo import api, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import clean_context


ACCOUNT_DEFAULTS = (
    ('baseer_salary_expense_id', 'sa_account_400003', '400090', 'Salary expense | مصروف الرواتب', 'expense', False),
    ('baseer_salary_payable_id', None, '201090', 'Salaries payable | رواتب مستحقة الدفع', 'liability_payable', True),
    ('baseer_deduction_account_id', 'sa_account_400074', '400091', 'Salary deductions | خصومات الرواتب', 'expense', False),
    ('baseer_loan_account_id', None, '102090', 'Employee advances | سلف الموظفين', 'asset_receivable', True),
    ('baseer_eos_expense_id', 'sa_account_400008', '400092', 'End-of-service expense | مصروف نهاية الخدمة', 'expense', False),
)


class Company(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        # Native create is authorized first. Native localization callbacks run first.
        @self.env.cr.precommit.add
        def complete_created_companies():
            companies.exists().sudo()._baseer_prepare_accounting()
        return companies

    @api.model
    def _baseer_initialize_accounting(self):
        self.sudo().with_context(active_test=False).search([])._baseer_prepare_accounting()

    def _baseer_chart_is_empty(self):
        self.ensure_one()
        children = self.with_context(active_test=False).search([('id', 'child_of', self.id)])
        for model, owner in (('account.account', 'company_ids'), ('account.journal', 'company_id'),
                             ('account.tax', 'company_id'), ('account.move', 'company_id')):
            if self.env[model].with_context(active_test=False).search_count([(owner, 'in', children.ids)], limit=1):
                return False
        return True

    def _baseer_seed_identity(self, key, model):
        self.ensure_one()
        data = self.env['ir.model.data'].search([
            ('module', '=', 'baseer_company_setup'), ('name', '=', f'{key}_company_{self.id}')], limit=1)
        record = self.env[model]
        if data:
            if data.model != model:
                raise ValidationError(_('The company accounting seed reference is invalid.'))
            record = record.browse(data.res_id).exists()
            owned = record and (self in record.company_ids if model == 'account.account' else record.company_id == self)
            if not owned:
                raise ValidationError(_('The company accounting seed reference is invalid.'))
        return record

    def _baseer_remember_seed(self, key, record):
        self.env['ir.model.data'].create({
            'module': 'baseer_company_setup', 'name': f'{key}_company_{self.id}',
            'model': record._name, 'res_id': record.id, 'noupdate': True,
        })
        return record

    def _baseer_default_account(self, field, native_id, code, name, kind, reconcile):
        account = self._baseer_seed_identity(field, 'account.account')
        if not account and native_id and self.chart_template == 'sa':
            candidate = self.env.ref(f'account.{self.id}_{native_id}', raise_if_not_found=False)
            if candidate and self in candidate.company_ids and candidate.account_type == kind and candidate.reconcile == reconcile and candidate.active:
                account = candidate
        if not account:
            accounts = self.env['account.account']
            code = accounts._search_new_account_code(code, cache=set())
            account = accounts.create({'name': name, 'code': code, 'company_ids': [(6, 0, self.ids)],
                                       'account_type': kind, 'reconcile': reconcile})
            self._baseer_remember_seed(field, account)
        if account.account_type != kind or account.reconcile != reconcile or not account.active:
            raise ValidationError(_('Review the archived or modified company accounting default: %s', account.display_name))
        return account

    def _baseer_default_journal(self, key, kind, code, name, reuse=True):
        journal = self._baseer_seed_identity(key, 'account.journal')
        if journal:
            if journal.type != kind:
                raise ValidationError(_('Review the modified company journal: %s', journal.display_name))
            return journal  # Keep renamed/archived seed journals; never replace them.
        journals = self.env['account.journal'].with_context(active_test=False)
        if reuse:
            journal = journals.search([('company_id', '=', self.id), ('type', '=', kind)], order='active desc, sequence, id', limit=1)
        if not journal:
            codes = set(journals.search([('company_id', '=', self.id)]).mapped('code'))
            if code in codes:
                code = next((f'BS{i:03}' for i in range(1, 1000) if f'BS{i:03}' not in codes), None)
                if not code:
                    raise ValidationError(_('No available company journal code.'))
            values = {'name': name, 'code': code, 'type': kind, 'company_id': self.id}
            if kind == 'sale':
                values['default_account_id'] = self.income_account_id.id
            elif kind == 'purchase':
                values['default_account_id'] = self.expense_account_id.id
            journal = self.env['account.journal'].create(values)
            self._baseer_remember_seed(key, journal)
        return journal

    def _baseer_prepare_accounting(self):
        for original in self.sorted('id'):
            company = original.with_context(dict(clean_context(self.env.context), allowed_company_ids=[original.id], active_test=False)).with_company(original)
            if company.parent_id:
                continue  # Native shared branch chart is not a separate payroll ledger.
            self.env.cr.execute('SELECT id FROM res_company WHERE id = %s FOR UPDATE', [company.id])
            company.invalidate_recordset()
            if not company.chart_template:
                if company.country_id.code == 'SA' and company.currency_id == company.country_id.currency_id and company._baseer_chart_is_empty():
                    # Dependency already installed; this never installs a module or commits.
                    company.env['account.chart.template']._load('sa', company, install_demo=False)
                continue
            updates = {}
            for definition in ACCOUNT_DEFAULTS:
                if not company[definition[0]]:
                    updates[definition[0]] = company._baseer_default_account(*definition).id
            starters = {}
            for key, kind, code, name in (
                ('sales', 'sale', 'INV', 'Sales | المبيعات'),
                ('purchases', 'purchase', 'BILL', 'Purchases | المشتريات'),
                ('bank', 'bank', 'BNK1', 'Bank | البنك'),
                ('cash', 'cash', 'CSH1', 'Cash | النقد'),
            ):
                starters[kind] = company._baseer_default_journal(key, kind, code, name)
            if not company.baseer_payroll_journal_id:
                payroll = company._baseer_default_journal('payroll', 'general', 'BPAY', 'Payroll | الرواتب', reuse=False)
                if not payroll.active:
                    raise ValidationError(_('Review the archived company payroll journal: %s', payroll.display_name))
                updates['baseer_payroll_journal_id'] = payroll.id
            if not company.baseer_eos_journal_id and starters['purchase'].active:
                updates['baseer_eos_journal_id'] = starters['purchase'].id
            if updates:
                company.write(updates)
            for journal in starters['bank'] | starters['cash']:
                treasury = journal.default_account_id
                if not journal.active or not treasury or treasury.account_type != 'asset_cash' or treasury.reconcile or company not in treasury.company_ids:
                    continue
                if company.env['account.move'].search_count([('journal_id', '=', journal.id)], limit=1) or company.env['account.payment'].search_count([('journal_id', '=', journal.id)], limit=1):
                    continue
                for method in journal.outbound_payment_method_line_ids.filtered(lambda m: m.code == 'manual' and not m.payment_account_id):
                    method.payment_account_id = treasury


class ChartTemplate(models.AbstractModel):
    _inherit = 'account.chart.template'

    def _load(self, template_code, company, install_demo, force_create=True):
        result = super()._load(template_code, company, install_demo, force_create)
        self.env['res.company'].browse(company if isinstance(company, int) else company.id).sudo()._baseer_prepare_accounting()
        return result
