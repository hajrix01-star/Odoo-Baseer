"""Native supplier bills from an atomic, company-scoped administrative batch.

The batch owns no alternative ledger, payment state or tax formula. Custom
money validation/summing uses Decimal; the existing Odoo tax and accounting
APIs receive/return native floats only at their established boundaries.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError


CENT = Decimal('0.01')
ZERO = Decimal('0.00')
MAX_ROWS = 50
MAX_REFERENCE_CANDIDATES = 10000


def monetary(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def checked_gross(value, env):
    if isinstance(value, bool):
        raise ValidationError(env._('Enter a positive gross amount with at most two decimal places.'))
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= ZERO or number > Decimal('999999999.99') or number != number.quantize(CENT):
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(env._('Enter a positive gross amount with at most two decimal places.')) from None
    return number.quantize(CENT)


def normalized_reference(value, env):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise ValidationError(env._('A supplier reference of 1 to 128 characters is required.'))
    return value.strip().casefold()


def company_scope(record, company):
    company = company.exists()
    if len(company) != 1 or company.id not in record.env.user.company_ids.ids:
        raise AccessError(record.env._('The company is outside your authorized companies.'))
    company.check_access('read')
    if company.currency_id.name != 'SAR':
        raise ValidationError(record.env._('Purchase batches currently support SAR companies only.'))
    # Discard caller-supplied financial bypass/default flags. Language and time
    # zone are presentation preferences; company is independently authorized.
    return record.with_context({
        'allowed_company_ids': company.ids,
        'lang': record.env.lang,
        'tz': record.env.user.tz or 'Asia/Riyadh',
    })


def require_active_company(record, company):
    """Check the caller's active company before any scoped context is built."""
    if len(company) != 1 or company != record.env.company:
        raise AccessError(record.env._('Switch to the batch company before creating or changing this batch.'))


def native_quote(gross, tax, product, partner, company):
    """Invert a configured simple tax without rounding the invoice unit price."""
    if not tax:
        return {'gross': gross, 'net': gross, 'tax': ZERO, 'unit_price': gross}
    tax.check_access('read')
    if (not tax.active or tax.company_id != company or tax.type_tax_use != 'purchase'
            or tax.amount_type != 'percent' or Decimal(str(tax.amount)) < ZERO
            or tax.include_base_amount or tax.has_negative_factor):
        raise ValidationError(company.env._('Choose one active, simple purchase percentage tax from this company.'))
    repartition = tax.invoice_repartition_line_ids.filtered(lambda line: line.repartition_type == 'tax')
    if sum((Decimal(str(line.factor_percent)) for line in repartition), ZERO) != Decimal('100'):
        raise ValidationError(company.env._('Reverse-charge and adjusted tax repartitions are not supported in purchase batches.'))
    inverse = tax.with_context(force_price_include=True, round_base=False).compute_all(
        float(gross), currency=company.currency_id, quantity=1.0,
        product=product or None, partner=partner or None, rounding_method='round_globally',
    )
    # price_unit in Odoo 19 has display precision, not a two-decimal storage
    # precision. Keeping this native inverse is essential for e.g. gross 0.04.
    unit = gross if tax.price_include else Decimal(str(inverse['total_excluded']))
    forward = tax.compute_all(float(unit), currency=company.currency_id, quantity=1.0,
                              product=product or None, partner=partner or None)
    if monetary(forward['total_included']) != gross:
        raise ValidationError(company.env._('The configured tax cannot reproduce this gross amount exactly.'))
    net = monetary(forward['total_excluded'])
    return {'gross': gross, 'net': net, 'tax': gross - net, 'unit_price': unit}


def is_principal_purchase_vat(tax, company):
    """Read-safe classification only; never resolve, create or rewrite a tax."""
    if (not tax or not company or tax != company.account_purchase_tax_id
            or not tax.active or tax.company_id != company or tax.type_tax_use != 'purchase'
            or tax.amount_type != 'percent' or Decimal(str(tax.amount)) != Decimal('15')
            or tax.include_base_amount or tax.has_negative_factor):
        return False
    repartition = tax.invoice_repartition_line_ids.filtered(lambda line: line.repartition_type == 'tax')
    return sum((Decimal(str(line.factor_percent)) for line in repartition), ZERO) == Decimal('100')


class BaseerPurchaseCategoryMap(models.Model):
    _name = 'baseer.purchase.category.map'
    _rec_name = 'category_id'
    _description = 'Purchase batch category mapping'
    _order = 'company_id, category_id, id'
    _check_company_auto = True

    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, ondelete='restrict')
    category_id = fields.Many2one('product.category', required=True, ondelete='restrict')
    parent_category_id = fields.Many2one(related='category_id.parent_id', string='Parent Category', readonly=True)
    product_id = fields.Many2one('product.product', required=True, ondelete='restrict', check_company=True,
                                 domain="[('type', '=', 'service'), ('categ_id', '=', category_id)]")
    active = fields.Boolean(default=True)
    _company_category_unique = models.Constraint('unique(company_id, category_id)',
                                                  'Each company can map a category only once.')

    @api.depends('category_id.name', 'category_id.complete_name')
    @api.depends_context('hierarchical_naming')
    def _compute_display_name(self):
        for record in self:
            record.display_name = record.category_id.display_name or _('Category mapping')

    def _validated_expense_account(self):
        self.ensure_one()
        scoped = company_scope(self, self.company_id)
        product = scoped.product_id
        product.check_access('read')
        if (not scoped.active or not product.active or product.type != 'service'
                or product.categ_id != scoped.category_id
                or product.company_id and product.company_id != scoped.company_id):
            raise ValidationError(_('The category requires an active service product in the same category and company.'))
        account = product.property_account_expense_id or product._get_category_account('property_account_expense_categ_id')
        if (not account or not account.active or scoped.company_id not in account.company_ids
                or account.account_type not in ('expense', 'expense_direct_cost', 'expense_depreciation')):
            raise ValidationError(_('Configure a valid expense account on the service product or its category.'))
        account.check_access('read')
        return account

    @api.constrains('company_id', 'category_id', 'product_id')
    def _check_mapping(self):
        for record in self:
            record._validated_expense_account()

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            company_scope(self, self.env['res.company'].browse(values.get('company_id') or self.env.company.id))
        return super().create(vals_list)

    def write(self, values):
        for record in self:
            company_scope(record, record.company_id)
        if values.get('company_id'):
            company_scope(self, self.env['res.company'].browse(values['company_id']))
        return super().write(values)


class BaseerPurchaseBatch(models.Model):
    _name = 'baseer.purchase.batch'
    _description = 'Purchase entry batch'
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True, default=lambda self: _('New'), copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, ondelete='restrict', index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    entry_date = fields.Date(required=True, default=fields.Date.context_today, index=True,
                             help='Administrative batch date. Invoice and payment accounting dates come from each row.')
    line_ids = fields.One2many('baseer.purchase.batch.line', 'batch_id', copy=True)
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved')], default='draft', required=True, readonly=True, copy=False, index=True)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    amount_gross = fields.Monetary(compute='_compute_amounts', currency_field='currency_id')
    amount_net = fields.Monetary(compute='_compute_amounts', currency_field='currency_id')
    amount_tax = fields.Monetary(compute='_compute_amounts', currency_field='currency_id')
    move_ids = fields.Many2many('account.move', compute='_compute_documents')
    bill_count = fields.Integer(compute='_compute_documents')
    payment_count = fields.Integer(compute='_compute_documents')
    has_custom_vat = fields.Boolean(compute='_compute_has_custom_vat')
    is_current_company = fields.Boolean(compute='_compute_is_current_company')

    _PROTECTED = frozenset({'name', 'state', 'approved_by_id', 'approved_at', 'amount_gross',
                            'amount_net', 'amount_tax', 'move_ids', 'bill_count', 'payment_count', 'currency_id',
                            'has_custom_vat', 'is_current_company'})

    @api.depends('company_id')
    @api.depends_context('company')
    def _compute_is_current_company(self):
        for batch in self:
            batch.is_current_company = batch.company_id == self.env.company

    @api.depends('line_ids.vat_is_custom')
    def _compute_has_custom_vat(self):
        for batch in self:
            batch.has_custom_vat = any(batch.line_ids.mapped('vat_is_custom'))

    @api.depends('line_ids.gross_amount', 'line_ids.net_amount', 'line_ids.tax_amount')
    def _compute_amounts(self):
        for batch in self:
            batch.amount_gross = float(sum((monetary(line.gross_amount) for line in batch.line_ids), ZERO))
            batch.amount_net = float(sum((monetary(line.net_amount) for line in batch.line_ids), ZERO))
            batch.amount_tax = float(sum((monetary(line.tax_amount) for line in batch.line_ids), ZERO))

    @api.depends('line_ids.move_id', 'line_ids.payment_id')
    def _compute_documents(self):
        for batch in self:
            batch.move_ids = batch.line_ids.move_id
            batch.bill_count = len(batch.move_ids)
            batch.payment_count = len(batch.line_ids.payment_id)

    def _lock_batches(self, operation='write'):
        if not self:
            return
        self.check_access(operation)
        for record in self:
            require_active_company(record, record.company_id)
            company_scope(record, record.company_id)
        self.flush_recordset(['state', 'company_id'])
        self.env.cr.execute('SELECT id FROM baseer_purchase_batch WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(sorted(self.ids))])
        self.invalidate_recordset(['state', 'company_id', 'line_ids'])

    def _require_draft(self):
        if any(batch.state != 'draft' for batch in self):
            raise UserError(_('Approved batches and their rows cannot be changed or deleted.'))

    def _check_row_limit(self):
        if any(len(batch.line_ids) > MAX_ROWS for batch in self):
            raise ValidationError(_('A purchase batch supports at most 50 rows.'))

    def _check_has_invoice_rows(self):
        if any(not batch.line_ids for batch in self):
            raise UserError(_('Add at least one invoice before saving this batch, or discard it to leave without saving.'))

    @api.model_create_multi
    def create(self, vals_list):
        values_to_create = []
        for values in vals_list:
            if self._PROTECTED.intersection(values):
                raise AccessError(_('Approval state, totals and document links are controlled by the server.'))
            company = self.env['res.company'].browse(values.get('company_id') or self.env.company.id)
            require_active_company(self, company)
            company_scope(self, company)
            values_to_create.append(dict(values, company_id=company.id))
        clean_self = self.with_context({
            'allowed_company_ids': sorted({values['company_id'] for values in values_to_create}),
            'lang': self.env.lang, 'tz': self.env.user.tz or 'Asia/Riyadh',
        })
        with self.env.cr.savepoint():
            # Caller default_state/default_approved_by/default_name cannot
            # populate otherwise protected fields through ORM defaults.
            records = super(BaseerPurchaseBatch, clean_self).create(values_to_create)
            records._check_has_invoice_rows()
            for batch in records:
                scoped = company_scope(batch, batch.company_id)
                name = scoped.env['ir.sequence'].next_by_code('baseer.purchase.batch')
                if not name:
                    raise UserError(_('The purchase batch sequence is not configured.'))
                super(BaseerPurchaseBatch, batch).write({'name': name})
            records._check_row_limit()
            return records

    def write(self, values):
        if self._PROTECTED.intersection(values):
            raise AccessError(_('Approval state, totals and document links are controlled by the server.'))
        with self.env.cr.savepoint():
            self._lock_batches()
            self._require_draft()
            if 'company_id' in values and any(batch.company_id.id != values['company_id'] for batch in self):
                raise UserError(_('The batch company cannot be changed. Create a new batch in the active company.'))
            result = super().write(values)
            self._check_has_invoice_rows()
            self._check_row_limit()
            return result

    def unlink(self):
        self._lock_batches('unlink')
        self._require_draft()
        return super().unlink()

    def _check_duplicate_references(self):
        self.ensure_one()
        keys = set()
        for line in self.line_ids:
            key = (line.partner_id.commercial_partner_id.id, normalized_reference(line.supplier_ref, self.env))
            if key in keys:
                raise ValidationError(_('The same supplier reference appears more than once in this batch.'))
            keys.add(key)
        candidates = self.env['account.move'].search([
            ('company_id', '=', self.company_id.id),
            ('commercial_partner_id', 'in', [partner for partner, reference in keys]),
            ('move_type', 'in', ['in_invoice', 'in_refund']), ('state', 'in', ['draft', 'posted']),
        ], limit=MAX_REFERENCE_CANDIDATES + 1)
        if len(candidates) > MAX_REFERENCE_CANDIDATES:
            raise UserError(_('Supplier history exceeds the batch duplicate-check limit. Use native invoicing for this batch.'))
        for move in candidates:
            if move.ref and (move.commercial_partner_id.id, move.ref.strip().casefold()) in keys:
                raise ValidationError(_('A draft or posted supplier document already uses this reference in this company.'))

    def action_approve(self):
        self.ensure_one()
        # Savepoint is deliberate: callers catching UserError in-process must
        # not retain invoices/payments from earlier successful rows.
        with self.env.cr.savepoint():
            self._lock_batches()
            if self.state == 'approved':
                return self.action_view_bills()
            self._require_draft()
            self._check_row_limit()
            if not self.line_ids:
                raise UserError(_('Add at least one purchase row before approval.'))
            if not self.env.user.has_group('account.group_account_invoice'):
                raise AccessError(_('You do not have permission to post supplier bills.'))
            batch = company_scope(self, self.company_id)
            # Serializes reference checking across this batch interface. Core
            # Odoo's own duplicate warning remains in place for outside entry.
            batch.env.cr.execute('SELECT id FROM res_company WHERE id = %s FOR UPDATE', [batch.company_id.id])
            # Odoo uses repeatable-read transactions. A no-op row update makes
            # this mutex an MVCC write conflict: a waiting concurrent approval
            # retries with a fresh snapshot instead of missing the first bill.
            # No company business value, write_date or configuration is changed.
            batch.env.cr.execute('UPDATE res_company SET id = id WHERE id = %s', [batch.company_id.id])
            prepared = [(line, line._prepare_approval()) for line in batch.line_ids.sorted(lambda line: (line.sequence, line.id))]
            batch._check_duplicate_references()
            for line, values in prepared:
                bill = batch.env['account.move'].create(values['bill'])
                if bill._get_violated_lock_dates(line.invoice_date, bool(line.tax_id)):
                    raise ValidationError(_('The invoice date is locked; the batch will not shift accounting dates automatically.'))
                if monetary(bill.amount_total) != values['quote']['gross']:
                    raise ValidationError(_('The native supplier bill total does not match the entered gross amount.'))
                if (monetary(bill.amount_untaxed) != values['quote']['net']
                        or monetary(bill.amount_tax) != values['quote']['tax']):
                    raise ValidationError(_('Native bill net and tax amounts must match the batch preview exactly.'))
                bill.action_post()
                if bill.state != 'posted' or bill.date != line.invoice_date or bill.invoice_date != line.invoice_date:
                    raise ValidationError(_('Native posting changed the requested invoice date. The entire batch was cancelled.'))
                if monetary(bill.amount_total) != values['quote']['gross']:
                    raise ValidationError(_('The posted supplier bill total does not match the entered gross amount.'))
                if (monetary(bill.amount_untaxed) != values['quote']['net']
                        or monetary(bill.amount_tax) != values['quote']['tax']):
                    raise ValidationError(_('Native bill net and tax amounts must match the batch preview exactly.'))
                payment = batch.env['account.payment']
                if line.payment_method_line_id:
                    method = line.payment_method_line_id
                    if batch.company_id._get_violated_lock_dates(line.invoice_date, False, method.journal_id):
                        raise ValidationError(_('The payment date is locked. Choose an open accounting date.'))
                    wizard = batch.env['account.payment.register'].with_context(
                        active_model='account.move', active_ids=bill.ids).create({
                            'journal_id': method.journal_id.id, 'payment_method_line_id': method.id,
                            'amount': bill.amount_total, 'payment_date': line.invoice_date,
                            'installments_mode': 'full', 'payment_difference_handling': 'open',
                        })
                    payment = wizard._create_payments()
                    line._verify_native_payment(bill, payment, values['quote']['gross'])
                super(BaseerPurchaseBatchLine, line).write({
                    'move_id': bill.id, 'payment_id': payment.id if payment else False,
                    'net_amount': bill.amount_untaxed, 'tax_amount': bill.amount_tax,
                })
            super(BaseerPurchaseBatch, batch).write({
                'state': 'approved', 'approved_by_id': self.env.uid, 'approved_at': fields.Datetime.now(),
            })
            return batch.action_view_bills()

    def action_view_bills(self):
        self.ensure_one()
        self.check_access('read')
        batch = company_scope(self, self.company_id)
        moves = batch.line_ids.move_id
        moves.check_access('read')
        action = {'type': 'ir.actions.act_window', 'name': _('Supplier bills'),
                  'res_model': 'account.move', 'view_mode': 'list,form',
                  'domain': [('id', 'in', moves.ids)], 'context': {'allowed_company_ids': batch.company_id.ids, 'create': False}}
        if len(moves) == 1:
            action.update(view_mode='form', res_id=moves.id, views=[(False, 'form')])
        return action

    def action_print(self):
        self.check_access('read')
        for batch in self:
            company_scope(batch, batch.company_id)
        return self.env.ref('baseer_purchase_batch.action_report_purchase_batch').report_action(self)

    def _get_print_data(self):
        self.ensure_one()
        self.check_access('read')
        batch = company_scope(self, self.company_id)
        rows = []
        for line in batch.line_ids.sorted(lambda line: (line.sequence, line.id)):
            rows.append({
                'date': fields.Date.to_string(line.invoice_date), 'supplier': line.partner_id.display_name,
                'reference': line.supplier_ref, 'type': _('Purchase') if line.entry_type == 'purchase' else _('Expense'),
                'category': line.category_map_id.display_name, 'description': line.description or line.category_map_id.display_name,
                'gross': format(monetary(line.gross_amount), '.2f'), 'net': format(monetary(line.net_amount), '.2f'),
                'tax': format(monetary(line.tax_amount), '.2f'), 'tax_name': line.tax_id.name or _('No tax'),
                'payment': line.payment_method_line_id.display_name or _('Credit'),
                'invoice': line.move_id.name or '',
            })
        return {'name': batch.name, 'company': batch.company_id.name, 'entry_date': fields.Date.to_string(batch.entry_date),
                'state': _('Approved') if batch.state == 'approved' else _('Draft'), 'rows': rows,
                'totals': {'gross': format(monetary(batch.amount_gross), '.2f'),
                           'net': format(monetary(batch.amount_net), '.2f'), 'tax': format(monetary(batch.amount_tax), '.2f')}}


class BaseerPurchaseBatchLine(models.Model):
    _name = 'baseer.purchase.batch.line'
    _rec_name = 'supplier_ref'
    _description = 'Purchase batch row'
    _order = 'sequence, id'
    _check_company_auto = True

    batch_id = fields.Many2one('baseer.purchase.batch', required=True, ondelete='cascade', index=True)
    batch_state = fields.Selection(related='batch_id.state', readonly=True)
    is_current_company = fields.Boolean(related='batch_id.is_current_company', readonly=True)
    company_id = fields.Many2one(related='batch_id.company_id', store=True, readonly=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    sequence = fields.Integer(default=10)
    invoice_date = fields.Date(required=True, default=fields.Date.context_today)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict', check_company=True)
    supplier_ref = fields.Char(required=True, size=128)
    entry_type = fields.Selection([('purchase', 'Purchase'), ('expense', 'Expense')], required=True, default='purchase')
    category_map_id = fields.Many2one('baseer.purchase.category.map', required=True, ondelete='restrict', check_company=True)
    description = fields.Char()
    gross_amount = fields.Monetary(required=True, currency_field='currency_id')
    tax_id = fields.Many2one('account.tax', ondelete='restrict', check_company=True)
    vat_enabled = fields.Boolean(string='VAT 15%', compute='_compute_vat_selection',
                                 inverse='_inverse_vat_enabled', readonly=False, copy=False)
    vat_is_custom = fields.Boolean(compute='_compute_vat_selection', readonly=True)
    net_amount = fields.Monetary(compute='_compute_amounts', store=True, currency_field='currency_id', readonly=True)
    tax_amount = fields.Monetary(compute='_compute_amounts', store=True, currency_field='currency_id', readonly=True)
    payment_method_line_id = fields.Many2one('account.payment.method.line', ondelete='restrict', check_company=True)
    is_credit = fields.Boolean(string='Credit', default=False)
    move_id = fields.Many2one('account.move', readonly=True, copy=False, ondelete='restrict', check_company=True)
    payment_id = fields.Many2one('account.payment', readonly=True, copy=False, ondelete='restrict', check_company=True)
    bill_payment_state = fields.Selection(related='move_id.payment_state', readonly=True)

    _PROTECTED = frozenset({'company_id', 'currency_id', 'batch_state', 'net_amount', 'tax_amount',
                            'move_id', 'payment_id', 'bill_payment_state', 'vat_is_custom', 'is_current_company'})

    def _supplier_default_category(self, partner):
        self.ensure_one()
        require_active_company(self, self.company_id)
        scoped = company_scope(self, self.company_id)
        partner = partner.with_env(scoped.env)
        partner.check_access('read')
        if not partner or any(record.company_id and record.company_id != scoped.company_id
                              for record in partner | partner.commercial_partner_id):
            return scoped.env['baseer.purchase.category.map']
        mapping = partner.baseer_purchase_category_map_id
        if mapping:
            mapping.check_access('read')
            if mapping.company_id != scoped.company_id or not mapping.active:
                raise ValidationError(_('The supplier default category must be active and belong to the batch company.'))
        return mapping

    @api.onchange('partner_id')
    def _onchange_partner_default_category(self):
        for line in self:
            if line.company_id:
                line.category_map_id = line._supplier_default_category(line.partner_id)

    @api.onchange('is_credit')
    def _onchange_is_credit(self):
        for line in self:
            if line.is_credit:
                line.payment_method_line_id = False

    def _normalize_entry_values(self, values, creating=False):
        self.ensure_one()
        result = self._normalize_vat_values(values)
        if 'is_credit' in result and not isinstance(result['is_credit'], bool):
            raise ValidationError(_('The credit switch must be true or false.'))
        credit = result.get('is_credit', False if creating else self.is_credit)
        if credit and result.get('payment_method_line_id'):
            raise ValidationError(_('A credit row cannot also specify a payment method.'))
        if result.get('is_credit'):
            result['payment_method_line_id'] = False
        if creating and 'category_map_id' not in result:
            partner = self.env['res.partner'].browse(result.get('partner_id'))
            mapping = self._supplier_default_category(partner)
            if mapping:
                result['category_map_id'] = mapping.id
        return result

    @api.depends('tax_id', 'tax_id.active', 'tax_id.amount', 'tax_id.amount_type',
                 'tax_id.type_tax_use', 'tax_id.company_id', 'tax_id.include_base_amount',
                 'tax_id.has_negative_factor', 'tax_id.invoice_repartition_line_ids.factor_percent',
                 'tax_id.invoice_repartition_line_ids.repartition_type', 'company_id.account_purchase_tax_id')
    def _compute_vat_selection(self):
        for line in self:
            # Existing taxes, including historic 0%/5%/archived taxes, remain
            # untouched. The UI hides the 15% switch and shows the actual tax
            # when this is a legacy/custom selection.
            principal_vat = is_principal_purchase_vat(line.tax_id, line.company_id)
            line.vat_enabled = principal_vat
            line.vat_is_custom = bool(line.tax_id) and not principal_vat

    def _vat_tax_for_value(self, enabled):
        self.ensure_one()
        if not isinstance(enabled, bool):
            raise ValidationError(_('The VAT switch must be true or false.'))
        if not enabled:
            return self.env['account.tax']
        if not self.company_id:
            raise ValidationError(_('Choose the batch company before enabling VAT.'))
        line = company_scope(self, self.company_id)
        tax = line.company_id.account_purchase_tax_id
        if tax:
            tax.check_access('read')
        if not is_principal_purchase_vat(tax, line.company_id):
            raise ValidationError(_('Configure an active simple 15% default purchase tax for this company before enabling VAT.'))
        return tax

    def _normalize_vat_values(self, values):
        """Normalize explicit UI/RPC intent; absent switches preserve tax_id."""
        self.ensure_one()
        if 'vat_enabled' not in values:
            return dict(values)
        tax = self._vat_tax_for_value(values['vat_enabled'])
        expected = tax.id if tax else False
        if 'tax_id' in values:
            given = values['tax_id']
            if (given not in (False, None) and (not isinstance(given, int) or isinstance(given, bool))) or given != expected:
                raise ValidationError(_('The VAT switch and tax selection contradict each other.'))
        result = dict(values)
        result.pop('vat_enabled')
        result['tax_id'] = expected
        return result

    @api.onchange('vat_enabled')
    def _onchange_vat_enabled(self):
        for line in self:
            if line.batch_state == 'approved':
                raise UserError(_('Approved batches and their rows cannot be changed or deleted.'))
            line.tax_id = line._vat_tax_for_value(line.vat_enabled)

    def _inverse_vat_enabled(self):
        # Public create/write normalize before ORM inverse processing; retain
        # a proper native inverse for other ORM callers, through draft guards.
        for line in self:
            enabled = line.vat_enabled
            line.write({'tax_id': line._vat_tax_for_value(enabled).id or False})

    @api.depends('gross_amount', 'tax_id', 'category_map_id', 'partner_id', 'company_id')
    def _compute_amounts(self):
        for line in self:
            if not line.gross_amount or not line.company_id:
                line.net_amount = 0.0
                line.tax_amount = 0.0
                continue
            scoped = company_scope(line, line.company_id)
            quote = native_quote(checked_gross(scoped.gross_amount, self.env), scoped.tax_id,
                                 scoped.category_map_id.product_id, scoped.partner_id, scoped.company_id)
            line.net_amount = float(quote['net'])
            line.tax_amount = float(quote['tax'])

    def _validate_inputs(self, strict_supplier=False):
        for line in self:
            checked_gross(line.gross_amount, self.env)
            normalized_reference(line.supplier_ref, self.env)
            scoped = company_scope(line, line.company_id)
            if scoped.category_map_id.company_id != scoped.company_id:
                raise ValidationError(_('Choose a category mapping from the batch company.'))
            scoped.category_map_id._validated_expense_account()
            partner = scoped.partner_id
            partner.check_access('read')
            if strict_supplier and partner.company_id and partner.company_id != scoped.company_id:
                raise ValidationError(_('Choose a shared supplier or a supplier from the batch company.'))
            if not partner.active or any(record.company_id and record.company_id != scoped.company_id
                                         for record in partner | partner.commercial_partner_id):
                raise ValidationError(_('Choose an active supplier accessible to the batch company.'))
            native_quote(checked_gross(scoped.gross_amount, self.env), scoped.tax_id,
                         scoped.category_map_id.product_id, partner, scoped.company_id)
            if scoped.is_credit and scoped.payment_method_line_id:
                raise ValidationError(_('A credit row cannot also specify a payment method.'))
            if not scoped.is_credit and not scoped.payment_method_line_id:
                raise ValidationError(_('Choose a payment method or explicitly enable Credit before saving.'))
            if scoped.payment_method_line_id:
                scoped._validate_payment_method()

    def _validate_payment_method(self):
        self.ensure_one()
        method = self.payment_method_line_id
        method.check_access('read')
        journal = method.journal_id
        if journal.currency_id and journal.currency_id != self.currency_id:
            raise ValidationError(_('Choose a payment journal in the company currency.'))
        if (method.company_id != self.company_id or method.payment_type != 'outbound'
                or method.code != 'manual' or not journal.active or journal.type not in ('cash', 'bank')
                or not method.payment_account_id or method.payment_account_id != journal.default_account_id
                or method.payment_account_id.account_type != 'asset_cash'):
            raise ValidationError(_('Choose a manual cash or bank method configured to post directly to its journal liquidity account; otherwise explicitly enable Credit.'))

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if self._PROTECTED.intersection(values):
                raise AccessError(_('Row totals, approval state and accounting links are controlled by the server.'))
            checked_gross(values.get('gross_amount'), self.env)
            normalized_reference(values.get('supplier_ref'), self.env)
        batches = self.env['baseer.purchase.batch'].browse(sorted({values.get('batch_id') for values in vals_list if values.get('batch_id')}))
        if any(not values.get('batch_id') for values in vals_list):
            raise ValidationError(_('Each row must belong to a saved draft batch.'))
        with self.env.cr.savepoint():
            batches._lock_batches()
            batches._require_draft()
            clean_self = self.with_context({
                'allowed_company_ids': sorted(batches.company_id.ids),
                'lang': self.env.lang, 'tz': self.env.user.tz or 'Asia/Riyadh',
            })
            normalized_values = []
            for values in vals_list:
                # A non-persistent prototype supplies only the already
                # authorized batch company for explicit input resolution.
                prototype = clean_self.new({'batch_id': values['batch_id']})
                normalized_values.append(prototype._normalize_entry_values(values, creating=True))
            # Strip caller defaults such as default_move_id/default_payment_id.
            records = super(BaseerPurchaseBatchLine, clean_self).create(normalized_values)
            records._validate_inputs(strict_supplier=True)
            batches._check_row_limit()
            return records

    def write(self, values):
        if self._PROTECTED.intersection(values):
            raise AccessError(_('Row totals, approval state and accounting links are controlled by the server.'))
        if 'gross_amount' in values:
            checked_gross(values['gross_amount'], self.env)
        if 'supplier_ref' in values:
            normalized_reference(values['supplier_ref'], self.env)
        self.check_access('write')
        batches = self.batch_id
        if values.get('batch_id'):
            batches |= self.env['baseer.purchase.batch'].browse(values['batch_id'])
        with self.env.cr.savepoint():
            batches._lock_batches()
            batches._require_draft()
            if 'batch_id' in values and any(line.batch_id.id != values['batch_id'] for line in self):
                raise UserError(_('Rows cannot be moved between batches. Create a new draft row instead.'))
            for line in self:
                super(BaseerPurchaseBatchLine, line).write(line._normalize_entry_values(values))
            result = True
            self._validate_inputs(strict_supplier='partner_id' in values)
            batches._check_row_limit()
            return result

    def unlink(self):
        self.check_access('unlink')
        self.batch_id._lock_batches()
        self.batch_id._require_draft()
        return super().unlink()

    def _prepare_approval(self):
        self.ensure_one()
        self._validate_inputs(strict_supplier=True)
        line = company_scope(self, self.company_id)
        quote = native_quote(checked_gross(line.gross_amount, self.env), line.tax_id,
                             line.category_map_id.product_id, line.partner_id, line.company_id)
        account = line.category_map_id._validated_expense_account()
        return {'quote': quote, 'bill': {
            'move_type': 'in_invoice', 'company_id': line.company_id.id, 'currency_id': line.currency_id.id,
            'partner_id': line.partner_id.id, 'ref': line.supplier_ref.strip(),
            'invoice_date': line.invoice_date, 'date': line.invoice_date, 'auto_post': 'no',
            'invoice_line_ids': [Command.create({
                'product_id': line.category_map_id.product_id.id,
                'name': line.description or line.category_map_id.category_id.complete_name,
                'quantity': 1.0, 'price_unit': float(quote['unit_price']), 'account_id': account.id,
                'tax_ids': [Command.set(line.tax_id.ids)],
            })],
        }}

    def _verify_native_payment(self, bill, payment, gross):
        self.ensure_one()
        journal = self.payment_method_line_id.journal_id
        if len(payment) != 1 or not payment.move_id or payment.move_id.state != 'posted' or payment.move_id.date != self.invoice_date:
            raise ValidationError(_('Native payment did not create one posted entry on the requested date.'))
        liquidity = payment.move_id.line_ids.filtered(lambda line: line.account_id == journal.default_account_id)
        paid = -sum((monetary(line.balance) for line in liquidity), ZERO)
        payable = bill.line_ids.filtered(lambda line: line.account_id.account_type == 'liability_payable')
        if (paid != gross or monetary(bill.amount_residual) != ZERO or not payable or not all(payable.mapped('reconciled'))
                or bill.payment_state != 'paid' or payment.state != 'paid'):
            raise ValidationError(_('The invoice is not fully paid and reconciled through actual cash or bank entries.'))

    def action_view_bill(self):
        self.ensure_one()
        self.check_access('read')
        line = company_scope(self, self.company_id)
        line.move_id.check_access('read')
        action = {'type': 'ir.actions.act_window', 'name': _('Supplier bill'), 'res_model': 'account.move',
                  'view_mode': 'list,form', 'domain': [('id', 'in', line.move_id.ids)],
                  'context': {'allowed_company_ids': line.company_id.ids, 'create': False}}
        if line.move_id:
            action.update(view_mode='form', res_id=line.move_id.id, views=[(False, 'form')])
        return action
