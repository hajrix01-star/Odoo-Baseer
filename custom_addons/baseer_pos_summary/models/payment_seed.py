"""Company-scoped, fill-once POS defaults; native ledger remains authoritative."""
import logging
from odoo import api, Command, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import clean_context


APPLICATIONS = (
    ('hungerstation', 'هنقرستيشن | HungerStation', '102110'),
    ('keeta', 'كيتا | Keeta', '102111'),
    ('jahez', 'جاهز | Jahez', '102112'),
)
_logger = logging.getLogger(__name__)


class Company(models.Model):
    _inherit = 'res.company'

    def _baseer_prepare_accounting(self):
        result = super()._baseer_prepare_accounting()
        self._baseer_seed_pos_payments()
        return result

    @api.model
    def _baseer_initialize_pos_payments(self):
        self.sudo().with_context(active_test=False).search([])._baseer_seed_pos_payments()

    def _baseer_pos_identity(self, key, model, record=None):
        self.ensure_one()
        name = f'payment_seed_{key}_company_{self.id}'
        data = self.env['ir.model.data'].search([
            ('module', '=', 'baseer_pos_summary'), ('name', '=', name)], limit=1)
        if data:
            existing = self.env[model].browse(data.res_id).exists() if data.model == model else self.env[model]
            owned = existing and (self in existing.company_ids if model == 'account.account' else existing.company_id == self)
            if not owned:
                raise ValidationError(_('Invalid POS payment seed reference: %s', name))
            return existing
        if record:
            self.env['ir.model.data'].create({'module': 'baseer_pos_summary', 'name': name,
                'model': model, 'res_id': record.id, 'noupdate': True})
            return record
        return self.env[model]

    def _baseer_pos_account(self, key, code, name, kind, reconcile=True):
        account = self._baseer_pos_identity(key, 'account.account')
        if not account:
            accounts = self.env['account.account']
            account = accounts.create({'name': name,
                'code': accounts._search_new_account_code(code, cache=set()),
                'account_type': kind, 'reconcile': reconcile, 'company_ids': [Command.set(self.ids)]})
            self._baseer_pos_identity(key, account._name, account)
        return account

    def _baseer_pos_journal(self, key, code, name, kind, account=None):
        journal = self._baseer_pos_identity(key, 'account.journal')
        if not journal:
            codes = set(self.env['account.journal'].search([('company_id', '=', self.id)]).mapped('code'))
            if code in codes:
                code = next((f'PS{i:03}' for i in range(1, 1000) if f'PS{i:03}' not in codes), None)
                if not code:
                    raise ValidationError(_('No available POS seed journal code.'))
            values = {'name': name, 'code': code, 'type': kind, 'company_id': self.id}
            if account:
                values['default_account_id'] = account.id
            journal = self.env['account.journal'].create(values)
            self._baseer_pos_identity(key, journal._name, journal)
        return journal

    def _baseer_pos_category(self, kind, name, sequence):
        category = self._baseer_pos_identity('category_' + kind, 'baseer.pos.payment.category')
        if not category:
            category = self.env['baseer.pos.payment.category'].create({
                'name': name, 'kind': kind, 'sequence': sequence, 'company_id': self.id})
            self._baseer_pos_identity('category_' + kind, category._name, category)
        return category

    def _baseer_pos_method(self, key, name, category, journal, receivable, outstanding=None):
        method = self._baseer_pos_identity('method_' + key, 'pos.payment.method')
        if not method:
            method = self.env['pos.payment.method'].create({
                'name': name, 'company_id': self.id, 'baseer_category_id': category.id,
                'journal_id': journal.id, 'receivable_account_id': receivable.id,
                'outstanding_account_id': outstanding.id if outstanding else False,
                'split_transactions': False, 'payment_method_type': 'none',
                'sequence': category.sequence})
            self._baseer_pos_identity('method_' + key, method._name, method)
        return method

    def _baseer_seed_pos_payments(self):
        for original in self.sorted('id'):
            if original.parent_id or not original.chart_template or original.currency_id.name != 'SAR':
                continue
            company = original.sudo().with_context(dict(clean_context(self.env.context),
                allowed_company_ids=[original.id], active_test=False, lang='en_US')).with_company(original)
            company.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [company.id])
            company.invalidate_recordset()
            # Once configured, user edits and archives remain authoritative.
            if company._baseer_pos_identity('config', 'pos.config'):
                continue
            company._baseer_create_pos_defaults()

    def _baseer_create_pos_defaults(self):
        self.ensure_one()
        config = self.env['pos.config'].search([
            ('company_id', '=', self.id), ('baseer_summary_only', '=', True)], limit=1)
        if config and not config.active:
            self._baseer_pos_identity('config', config._name, config)
            return
        has_history = config and (self.env['pos.session'].search_count([('config_id', '=', config.id)], limit=1)
            or self.env['baseer.pos.summary'].search_count([('config_id', '=', config.id)], limit=1))
        # Never silently change the setup behind existing transactions or drafts.
        if has_history:
            try:
                config._validate_baseer_setup()
            except ValidationError as error:
                _logger.warning('POS payment seed deferred for company %s, config %s: %s', self.id, config.id, error)
                return
            existing_kinds = set(config.payment_method_ids.mapped('baseer_category_id.kind'))
            additions = 3 + int('cash' not in existing_kinds) + int('bank' not in existing_kinds)
            if len(config.payment_method_ids) + additions > 25:
                _logger.warning('POS payment seed deferred for company %s: insufficient capacity for three application methods', self.id)
                return
        categories = {kind: self._baseer_pos_category(kind, name, seq) for kind, name, seq in (
            ('cash', 'نقدي | Cash', 10), ('bank', 'بنك | Bank', 20), ('platform', 'تطبيقات | Applications', 30))}
        receivable = self.account_default_pos_receivable_account_id
        if not (receivable and receivable.active and receivable.reconcile
                and receivable.account_type == 'asset_receivable' and self in receivable.company_ids):
            receivable = self._baseer_pos_account('receivable', '102095',
                'وسيط ملخص المبيعات | Sales summary intermediary', 'asset_receivable')
        bank_account = self._baseer_pos_account('bank_account', '101090',
            'البنك - ملخص المبيعات | Bank - sales summaries', 'asset_cash')
        bank = self._baseer_pos_journal('bank_journal', 'PSBNK',
            'تحصيلات ملخص المبيعات | Sales summary receipts', 'bank', bank_account)
        methods = config.payment_method_ids if has_history else self.env['pos.payment.method']
        if not methods.filtered(lambda m: m.baseer_category_id.kind == 'cash'):
            cash_account = self._baseer_pos_account('cash_account', '105090',
                'صندوق ملخص المبيعات | Sales summary cash', 'asset_cash', False)
            cash = self._baseer_pos_journal('cash_journal', 'PSCSH',
                'صندوق ملخص المبيعات | Sales summary cash', 'cash', cash_account)
            methods |= self._baseer_pos_method('cash', 'نقدي | Cash', categories['cash'], cash, receivable)
        if not methods.filtered(lambda m: m.baseer_category_id.kind == 'bank'):
            methods |= self._baseer_pos_method('bank', 'بنك | Bank', categories['bank'], bank, receivable, bank_account)
        for key, label, code in APPLICATIONS:
            arabic, english = label.split(' | ')
            account = self._baseer_pos_account(key + '_clearing', code,
                f'مستحقات {arabic} | {english} receivable', 'asset_current')
            methods |= self._baseer_pos_method(key, label, categories['platform'], bank, receivable, account)
        if not config:
            config = self.env['pos.config'].create({'name': 'ملخص المبيعات | Sales summaries',
                'company_id': self.id, 'baseer_summary_only': True,
                'payment_method_ids': [Command.set(methods.ids)]})
        else:
            config.write({'payment_method_ids': [Command.set(methods.ids)]})
        updates = {}
        if not config.baseer_summary_product_id:
            income = self.income_account_id
            if not income or income.account_type not in ('income', 'income_other'):
                income = self._baseer_pos_account('income', '500090',
                    'إيرادات ملخص المبيعات | Sales summary revenue', 'income', False)
            product = self.env['product.product'].create({'name': 'ملخص المبيعات | Sales summary',
                'type': 'service', 'company_id': self.id, 'sale_ok': True, 'purchase_ok': False,
                'property_account_income_id': income.id})
            updates['baseer_summary_product_id'] = product.id
        if not config.baseer_summary_tax_id:
            tax = self.account_sale_tax_id
            if not tax or tax.type_tax_use != 'sale':
                raise ValidationError(_('Configure the company sales tax before creating POS payment defaults.'))
            updates['baseer_summary_tax_id'] = tax.id
        if updates:
            config.write(updates)
        config._validate_baseer_setup()
        self._baseer_pos_identity('config', config._name, config)


class PosConfig(models.Model):
    _inherit = 'pos.config'

    def _get_group_pos_manager(self):
        # pos_hr.write otherwise creates a cashier employee for a company manager.
        # A summary-only POS never opens the cashier UI or needs cashier staff.
        # This must also cover deferred native recomputations after seed context
        # expires. Empty-record default resolution retains the real group field.
        if self and all(self.mapped('baseer_summary_only')):
            return self.env['res.groups']
        return super()._get_group_pos_manager()
