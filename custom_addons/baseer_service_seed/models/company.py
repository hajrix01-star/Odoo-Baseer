"""Fill-only company service masters; all financial posting remains native."""
from odoo import api, Command, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import clean_context

from .catalog import ACCOUNT_PURPOSES, PROVIDERS, SERVICES


class Company(models.Model):
    _inherit = 'res.company'

    def _baseer_prepare_accounting(self):
        result = super()._baseer_prepare_accounting()
        # Chart callbacks can run before module XML data has been loaded.
        if self.env.ref('baseer_service_seed.category_employee', raise_if_not_found=False):
            self._baseer_seed_services()
        return result

    @api.model
    def _baseer_initialize_services(self):
        self.sudo().with_context(active_test=False).search([])._baseer_seed_services()

    def _baseer_service_identity(self, kind, key, model, record=None):
        self.ensure_one()
        name = f'{kind}_{key}_company_{self.id}'
        data = self.env['ir.model.data'].search([
            ('module', '=', 'baseer_service_seed'), ('name', '=', name)], limit=1)
        if data:
            if data.model != model:
                raise ValidationError(_('The service seed reference has the wrong model: %s', name))
            existing = self.env[model].browse(data.res_id).exists()
            if not existing:
                raise ValidationError(_('A referenced service seed record was deleted: %s', name))
            owned = (self in existing.company_ids if model == 'account.account' else
                     existing.company_id in (self, self.env['res.company']) if kind == 'provider' and model == 'res.partner' else
                     existing.company_id == self)
            if not owned:
                raise ValidationError(_('The service seed reference belongs to another company: %s', name))
            return existing
        if record:
            self.env['ir.model.data'].create({
                'module': 'baseer_service_seed', 'name': name, 'model': model,
                'res_id': record.id, 'noupdate': True,
            })
            return record
        return self.env[model]

    def _baseer_service_account(self, purpose):
        account = self._baseer_service_identity('account', purpose, 'account.account')
        if account:
            return account
        native_code, fallback_code, label = ACCOUNT_PURPOSES[purpose]
        if self.chart_template == 'sa' and native_code:
            candidate = self.env.ref(f'account.{self.id}_sa_account_{native_code}', raise_if_not_found=False)
            if (candidate and self in candidate.company_ids and candidate.active
                    and candidate.account_type == 'expense'):
                account = candidate
        if not account:
            account_model = self.env['account.account']
            code = account_model._search_new_account_code(fallback_code, cache=set())
            account = account_model.create({
                'name': label, 'code': code, 'account_type': 'expense',
                'company_ids': [Command.set(self.ids)], 'reconcile': False,
            })
        return self._baseer_service_identity('account', purpose, 'account.account', account)

    def _baseer_service_mapping(self, service, accounts):
        key, english, arabic, purpose = service
        category = self.env.ref(f'baseer_service_seed.category_{key}')
        mapper = self.env['baseer.purchase.category.map']
        mapped = self._baseer_service_identity('mapping', key, mapper._name)
        if mapped:
            return mapped
        # Existing choices, including archived mappings, remain authoritative.
        mapped = mapper.search([('company_id', '=', self.id), ('category_id', '=', category.id)], limit=1)
        if mapped:
            return self._baseer_service_identity('mapping', key, mapper._name, mapped)
        product = self._baseer_service_identity('product', key, 'product.product')
        if not product:
            account = accounts.get(purpose)
            if account is None:
                account = accounts[purpose] = self._baseer_service_account(purpose)
            if not account.active or account.account_type != 'expense':
                raise ValidationError(_('Review the archived or changed service expense account: %s', account.display_name))
            product = self.env['product.product'].with_context(lang='en_US').create({
                'name': english, 'default_code': f'BASEER-SVC-{key.upper()}',
                'type': 'service', 'company_id': self.id, 'categ_id': category.id,
                'sale_ok': False, 'purchase_ok': True,
                'property_account_expense_id': account.id,
                'supplier_taxes_id': [Command.clear()], 'taxes_id': [Command.clear()],
            })
            # Only newly created products receive translated names.
            for language in self.env['res.lang'].search([('code', 'in', ['ar_001', 'ar']), ('active', '=', True)]):
                product.with_context(lang=language.code).name = arabic
            self._baseer_service_identity('product', key, product._name, product)
        if not product.active:
            # An archived seed is a deliberate choice, not permission to replace it.
            return mapper
        mapped = mapper.create({'company_id': self.id, 'category_id': category.id, 'product_id': product.id})
        return self._baseer_service_identity('mapping', key, mapper._name, mapped)

    def _baseer_service_provider(self, provider, mappings):
        return self._baseer_shared_service_provider(provider, mappings)

    def _baseer_seed_services(self):
        for original in self.sorted('id'):
            if original.parent_id or not original.chart_template or original.currency_id.name != 'SAR':
                continue
            company = original.sudo().with_context(
                dict(clean_context(self.env.context), allowed_company_ids=[original.id], active_test=False)
            ).with_company(original)
            company._baseer_lock_shared_providers()
            company.env.cr.execute('SELECT id FROM res_company WHERE id = %s FOR UPDATE', [company.id])
            # MVCC conflict forces concurrent Repeatable Read callers to retry
            # with a fresh snapshot. No business values or write_date are changed.
            company.env.cr.execute('UPDATE res_company SET id = id WHERE id = %s', [company.id])
            company.invalidate_recordset()
            accounts = {}
            mappings = {service[0]: company._baseer_service_mapping(service, accounts) for service in SERVICES}
            for provider in PROVIDERS:
                company._baseer_service_provider(provider, mappings)
