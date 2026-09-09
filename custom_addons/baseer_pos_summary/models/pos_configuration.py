from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .common import active_company, clean_context, internal, manager, native_quote, money


class PaymentCategory(models.Model):
    _name = 'baseer.pos.payment.category'
    _description = 'POS summary payment category'
    _order = 'sequence, id'
    _check_company_auto = True

    name = fields.Char(required=True, translate=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    kind = fields.Selection([('cash', 'Cash'), ('bank', 'Bank'), ('platform', 'Applications')], required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)

    @api.model_create_multi
    def create(self, vals_list):
        manager(self)
        for vals in vals_list:
            active_company(self, self.env['res.company'].browse(vals.get('company_id') or self.env.company.id))
        return super().create(vals_list)

    def write(self, vals):
        manager(self)
        self.check_access('write')
        for record in self:
            active_company(record, record.company_id)
            if 'company_id' in vals and vals['company_id'] != record.company_id.id:
                raise ValidationError(_('The payment category company cannot be changed.'))
        if 'kind' in vals:
            for company in self.company_id.sorted('id'):
                self.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [company.id])
        if 'kind' in vals and self.env['baseer.pos.summary.allocation'].search_count([
                ('category_id', 'in', self.ids), ('state', 'in', ['approved', 'cancelled'])], limit=1):
            raise ValidationError(_('A category used by approved summaries cannot be reclassified. Create a new category instead.'))
        return super().write(vals)

    def unlink(self):
        manager(self)
        for record in self:
            active_company(record, record.company_id)
        return super().unlink()


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    baseer_category_id = fields.Many2one('baseer.pos.payment.category', string='Summary Category', check_company=True, ondelete='restrict')

    @api.model_create_multi
    def create(self, vals_list):
        if any('baseer_category_id' in vals for vals in vals_list) or 'default_baseer_category_id' in self.env.context:
            manager(self)
        records = super().create(vals_list)
        records._check_baseer_category()
        return records

    def write(self, vals):
        if 'baseer_category_id' in vals:
            manager(self)
        semantic = {'baseer_category_id', 'company_id', 'journal_id', 'outstanding_account_id', 'receivable_account_id', 'split_transactions'}
        if semantic.intersection(vals) and (self.filtered('baseer_category_id') or vals.get('baseer_category_id')):
            manager(self)
            self.check_access('write')
            for company in self.company_id.sorted('id'):
                active_company(self, company)
                self.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [company.id])
        if semantic.intersection(vals) and self.env['baseer.pos.summary.allocation'].search_count([
                ('payment_method_id', 'in', self.ids), ('state', 'in', ['approved', 'cancelled'])], limit=1):
            raise ValidationError(_('Payment methods used by approved summaries cannot be reclassified or assigned different accounts. Create a new method instead.'))
        result = super().write(vals)
        if {'baseer_category_id', 'company_id', 'journal_id'}.intersection(vals):
            self._check_baseer_category()
        return result

    @api.constrains('baseer_category_id', 'company_id')
    def _check_baseer_category(self):
        for method in self:
            if method.baseer_category_id and method.baseer_category_id.company_id != method.company_id:
                raise ValidationError(_('The payment category must belong to the payment method company.'))

    def _validate_baseer_method(self, config):
        self.ensure_one()
        self.check_access('read')
        category = self.baseer_category_id
        journal = self.journal_id
        if (self not in config.payment_method_ids or self.company_id != config.company_id or not self.active
                or not category or not category.active or category.company_id != config.company_id
                or self.split_transactions or self.payment_method_type != 'none' or self.use_payment_terminal
                or not journal.active or journal.company_id != config.company_id
                or journal.type not in ('cash', 'bank') or journal.currency_id and journal.currency_id != config.currency_id):
            raise ValidationError(_('Choose an active manual summary payment method from this company, without split transactions or foreign currency.'))
        expected = 'cash' if category.kind == 'cash' else 'bank'
        if journal.type != expected:
            raise ValidationError(_('Cash categories require cash journals; bank and application categories require bank journals.'))
        receivable = self.receivable_account_id or config.company_id.account_default_pos_receivable_account_id
        if not receivable or not receivable.active or not receivable.reconcile or receivable.account_type != 'asset_receivable' or config.company_id not in receivable.company_ids:
            raise ValidationError(_('Configure an active reconcilable POS intermediary receivable account for this company.'))
        if journal.type == 'cash':
            if not journal.default_account_id or journal.default_account_id.account_type != 'asset_cash':
                raise ValidationError(_('The cash journal must have a liquidity account.'))
        else:
            account = self.outstanding_account_id
            if not account or not account.active or not account.reconcile or config.company_id not in account.company_ids:
                raise ValidationError(_('Configure an active reconcilable outstanding account for this payment method.'))
            if category.kind == 'bank' and account.account_type != 'asset_cash':
                raise ValidationError(_('Bank summary payments must post to an actual bank liquidity account. Use an application category for deferred platform clearing.'))
            if category.kind == 'platform' and account.account_type != 'asset_current':
                raise ValidationError(_('Application payments require a separate current-asset clearing account, not a cash account.'))
            if category.kind == 'platform' and any(other != self and other.outstanding_account_id == account for other in config.payment_method_ids):
                raise ValidationError(_('Each application must use its own distinct clearing account.'))


class PosConfig(models.Model):
    _inherit = 'pos.config'

    baseer_summary_only = fields.Boolean(string='External Summaries Only', copy=False)
    baseer_summary_product_id = fields.Many2one('product.product', string='Summary Service Product', check_company=True, ondelete='restrict')
    baseer_summary_tax_id = fields.Many2one('account.tax', string='Summary Sales Tax', check_company=True, ondelete='restrict')
    _baseer_summary_company_unique = models.UniqueIndex(
        '(company_id) WHERE baseer_summary_only IS TRUE', 'Only one dedicated summary POS may be configured per company.')
    _BASEER_FIELDS = {'baseer_summary_only', 'baseer_summary_product_id', 'baseer_summary_tax_id'}

    @api.model_create_multi
    def create(self, vals_list):
        if any(self._BASEER_FIELDS.intersection(vals) for vals in vals_list) or any('default_' + key in self.env.context for key in self._BASEER_FIELDS):
            manager(self)
        for vals in vals_list:
            product = self.env['product.product'].browse(vals.get('baseer_summary_product_id') or self.env.context.get('default_baseer_summary_product_id'))
            if product and product.type != 'service':
                raise ValidationError(_('The dedicated summary product must be a service. Use a separate product for stock operations.'))
        return super().create(vals_list)

    def write(self, vals):
        if self._BASEER_FIELDS.intersection(vals):
            manager(self)
        if vals.get('baseer_summary_product_id'):
            product = self.env['product.product'].browse(vals['baseer_summary_product_id'])
            if product.type != 'service':
                raise ValidationError(_('The dedicated summary product must be a service. Use a separate product for stock operations.'))
        if self.filtered('baseer_summary_only') and 'baseer_summary_only' in vals and not vals['baseer_summary_only']:
            raise ValidationError(_('A dedicated summary POS cannot be converted to a cashier POS.'))
        if 'company_id' in vals and any(config.baseer_summary_only and config.company_id.id != vals['company_id'] for config in self):
            raise ValidationError(_('The company of a dedicated summary POS cannot be changed.'))
        if vals.get('baseer_summary_only') and self.filtered(lambda c: not c.baseer_summary_only) and self.env['pos.session'].search_count([
                ('config_id', 'in', self.filtered(lambda c: not c.baseer_summary_only).ids)], limit=1):
            raise ValidationError(_('Create a new dedicated POS; an existing cashier session history cannot be converted to summaries.'))
        return super().write(vals)

    @api.model
    def _repair_summary_service_products(self):
        """Upgrade-only repair: never convert a stock product or its history."""
        configurations = self.with_context(active_test=False).search([('baseer_summary_only', '=', True)])
        for config in configurations:
            product = config.baseer_summary_product_id
            if product and product.type != 'service':
                replacement = product.with_company(config.company_id).copy({
                    'name': _('%s — summary service', product.name), 'type': 'service',
                    'company_id': config.company_id.id, 'active': True})
                config.with_company(config.company_id).write({'baseer_summary_product_id': replacement.id})

    def open_ui(self):
        if self.filtered('baseer_summary_only'):
            raise AccessError(_('Use External Sales Summaries to post this dedicated POS. Open an ordinary POS for direct sales.'))
        return super().open_ui()

    def _baseer_summary_dashboard_action(self, xmlid):
        self.ensure_one()
        self.check_access('read')
        active_company(self, self.company_id)
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessError(_('Point of Sale access is required.'))
        if not self.active or not self.baseer_summary_only:
            raise ValidationError(_('Choose an active summary configuration.'))
        action = self.env['ir.actions.actions']._for_xml_id(xmlid)
        action['context'] = dict(clean_context(self, self.company_id).env.context)
        return action

    def action_baseer_enter_summary(self):
        return self._baseer_summary_dashboard_action('baseer_pos_summary.action_pos_day_entry')

    def action_baseer_saved_summaries(self):
        action = self._baseer_summary_dashboard_action('baseer_pos_summary.action_summary')
        action['domain'] = [('company_id', '=', self.company_id.id)]
        action['context']['search_default_not_archived'] = 1
        return action

    def _validate_baseer_setup(self):
        self.ensure_one()
        active_company(self, self.company_id)
        self.check_access('read')
        if (not self.active or not self.baseer_summary_only or self.currency_id != self.company_id.currency_id
                or self.cash_rounding or self.default_fiscal_position_id or self.use_presets
                or not self.journal_id or not self.invoice_journal_id):
            raise ValidationError(_('Configure a dedicated SAR summary POS with journals, without cash rounding, fiscal positions or presets.'))
        for journal in self.journal_id | self.invoice_journal_id:
            if not journal.active or journal.company_id != self.company_id or journal.currency_id and journal.currency_id != self.currency_id:
                raise ValidationError(_('Summary journals must be active and belong to this company and currency.'))
        product = self.baseer_summary_product_id
        product.check_access('read')
        if not product or not product.active or product.type != 'service' or product.company_id and product.company_id != self.company_id:
            raise ValidationError(_('Configure an active summary service product for this company.'))
        account = product._get_product_accounts()['income']
        if not account or not account.active or account.account_type not in ('income', 'income_other') or self.company_id not in account.company_ids:
            raise ValidationError(_('Configure a valid income account on the summary service product or its category.'))
        native_quote(money(115), self.baseer_summary_tax_id, product, self.company_id)
        for method in self.payment_method_ids:
            method._validate_baseer_method(self)
        if not self.payment_method_ids or len(self.payment_method_ids) > 25 or len(self.payment_method_ids.filtered('is_cash_count')) > 1:
            raise ValidationError(_('Configure between 1 and 25 summary payment methods, including at most one cash method.'))


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        if vals.get('type') and vals['type'] != 'service':
            configs = self.env['pos.config'].sudo().with_context(active_test=False).search_count([
                ('baseer_summary_only', '=', True), ('baseer_summary_product_id.product_tmpl_id', 'in', self.ids)], limit=1)
            if configs:
                raise ValidationError(_('The dedicated summary product must remain a service. Assign a separate summary service before changing its type.'))
        return super().write(vals)

