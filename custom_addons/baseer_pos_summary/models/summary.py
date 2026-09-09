from datetime import datetime, time
from decimal import Decimal
from urllib.parse import urlencode

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError

from .common import ZERO, MAX_AMOUNT, money, positive_amount, active_company, clean_context, native_quote, lifecycle_manager, internal


class PosSummary(models.Model):
    _name = 'baseer.pos.summary'
    _description = 'External POS sales summary'
    _order = 'business_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, default=lambda self: _('New'), readonly=True, copy=False)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True, ondelete='restrict')
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    is_current_company = fields.Boolean(compute='_compute_current_company')
    business_date = fields.Date(required=True, default=fields.Date.context_today, index=True)
    period_scope = fields.Selection([('all', 'All Day'), ('morning', 'Morning'), ('evening', 'Evening')], required=True, default='all', index=True)
    external_reference = fields.Char(copy=False)
    day_schedule = fields.Selection([('all', 'Full day'), ('split', 'Morning and evening'), ('morning', 'Morning only'), ('evening', 'Evening only')], required=True, default='all', string='Day schedule')
    zero_sales = fields.Boolean(string='Worked with no sales', default=False)
    customer_count = fields.Integer(default=0, required=True)
    notes = fields.Text()
    attachment_ids = fields.Many2many('ir.attachment', string='Source Attachments')
    allocation_ids = fields.One2many('baseer.pos.summary.allocation', 'summary_id', copy=True)
    config_id = fields.Many2one('pos.config', required=True, default=lambda self: self._default_config(), check_company=True, ondelete='restrict')
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('cancelled', 'Cancelled for correction')], required=True, default='draft', readonly=True, copy=False, index=True)
    is_archived = fields.Boolean(default=False, readonly=True, copy=False, string='Archived')
    amount_gross = fields.Monetary(compute='_compute_totals', store=True, currency_field='currency_id')
    amount_net = fields.Monetary(compute='_compute_totals', store=True, currency_field='currency_id')
    amount_tax = fields.Monetary(compute='_compute_totals', store=True, currency_field='currency_id')
    average_per_customer = fields.Monetary(compute='_compute_average', currency_field='currency_id', aggregator=False)
    order_id = fields.Many2one('pos.order', readonly=True, copy=False, ondelete='restrict', check_company=True)
    session_id = fields.Many2one('pos.session', readonly=True, copy=False, ondelete='restrict', check_company=True)
    move_id = fields.Many2one(related='session_id.move_id', readonly=True)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    _order_unique = models.Constraint('unique(order_id)', 'A POS order may belong to only one external summary.')
    _session_unique = models.Constraint('unique(session_id)', 'A POS session may belong to only one external summary.')
    _active_company_date_period_unique = models.UniqueIndex("(company_id, business_date, period_scope) WHERE state != 'cancelled'", 'A summary already exists for this company, date and period.')
    _PROTECTED = {'name', 'state', 'currency_id', 'is_current_company', 'amount_gross', 'amount_net', 'amount_tax',
                  'average_per_customer', 'order_id', 'session_id', 'move_id', 'approved_by_id', 'approved_at', 'is_archived',
                  'correction_reason', 'corrected_by_id', 'corrected_at', 'replacement_id', 'replaces_id', 'reversal_move_ids'}

    def _write_correction_metadata(self, vals):
        if not internal(self):
            raise AccessError(_('Summary correction history is controlled by the server.'))
        return super().write(vals)

    @api.model
    def _default_config(self):
        return self.env['pos.config'].search([('company_id', '=', self.env.company.id), ('baseer_summary_only', '=', True)], limit=1)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if 'allocation_ids' in fields_list and not values.get('allocation_ids'):
            config = self.env['pos.config'].browse(values.get('config_id')) or self._default_config()
            methods = config.payment_method_ids.filtered(lambda m: m.active and m.baseer_category_id.active)
            values['allocation_ids'] = [Command.create({'payment_method_id': method.id, 'amount': 0}) for method in methods]
        return values

    @api.onchange('day_schedule')
    def _onchange_day_schedule(self):
        for summary in self:
            if summary.day_schedule != 'split':
                summary.period_scope = summary.day_schedule
            elif summary.period_scope == 'all':
                summary.period_scope = 'morning'

    @api.onchange('zero_sales')
    def _onchange_zero_sales(self):
        for summary in self.filtered('zero_sales'):
            summary.customer_count = 0
            for line in summary.allocation_ids:
                line.amount = 0

    @api.depends('company_id')
    @api.depends_context('company')
    def _compute_current_company(self):
        for summary in self:
            summary.is_current_company = summary.company_id == self.env.company

    @api.depends('allocation_ids.amount', 'customer_count', 'config_id.baseer_summary_tax_id', 'config_id.baseer_summary_product_id', 'order_id.amount_total', 'order_id.amount_tax', 'state')
    def _compute_totals(self):
        for summary in self:
            gross = sum((money(line.amount) for line in summary.allocation_ids), ZERO)
            net, tax = gross, ZERO
            if summary.state in ('approved', 'cancelled') and summary.order_id:
                gross = money(summary.order_id.amount_total)
                tax = money(summary.order_id.amount_tax)
                net = gross - tax
            elif gross and summary.config_id.baseer_summary_tax_id and summary.config_id.baseer_summary_product_id:
                scoped_config = summary.config_id.with_company(summary.company_id)
                quote = native_quote(gross, scoped_config.baseer_summary_tax_id, scoped_config.baseer_summary_product_id, summary.company_id)
                net, tax = quote['net'], quote['tax']
            summary.amount_gross, summary.amount_net, summary.amount_tax = float(gross), float(net), float(tax)

    @api.depends('amount_gross', 'customer_count')
    def _compute_average(self):
        for summary in self:
            summary.average_per_customer = float(money(money(summary.amount_gross) / Decimal(summary.customer_count))) if summary.customer_count else 0.0

    def _lock(self, operation='write'):
        self.check_access(operation)
        for summary in self:
            active_company(summary, summary.company_id)
        if self:
            self.flush_recordset(['state', 'company_id'])
            self.env.cr.execute('SELECT id FROM baseer_pos_summary WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(sorted(self.ids))])
            self.invalidate_recordset(['state', 'company_id', 'allocation_ids', 'order_id', 'session_id'])

    def _require_draft(self):
        if any(summary.state != 'draft' for summary in self):
            raise UserError(_('Approved external summaries cannot be changed or deleted.'))

    @api.model
    def _validate_values(self, vals):
        if 'customer_count' in vals and (not isinstance(vals['customer_count'], int) or isinstance(vals['customer_count'], bool) or not 0 <= vals['customer_count'] <= 10000000):
            raise ValidationError(_('Customer count must be a whole number between 0 and 10000000.'))
        if vals.get('external_reference') and (not isinstance(vals['external_reference'], str) or len(vals['external_reference'].strip()) > 128):
            raise ValidationError(_('The external reference cannot exceed 128 characters.'))
        if 'zero_sales' in vals and not isinstance(vals['zero_sales'], bool):
            raise ValidationError(_('The no-sales declaration must be enabled or disabled.'))

    def _serialize_company(self):
        self.ensure_one()
        # A real MVCC write forces a waiting approval to retry with a fresh
        # repeatable-read snapshot, so cross-period overlap checks see it.
        self.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [self.company_id.id])

    def _check_overlap(self):
        for summary in self:
            if (summary.day_schedule == 'split' and summary.period_scope == 'all') or (summary.day_schedule != 'split' and summary.period_scope != summary.day_schedule):
                raise ValidationError(_('The shift must match the selected day schedule.'))
            same_day = [('company_id', '=', summary.company_id.id), ('business_date', '=', summary.business_date), ('id', '!=', summary.id), ('state', '!=', 'cancelled')]
            if self.search_count(same_day + [('day_schedule', '!=', summary.day_schedule)], limit=1):
                raise ValidationError(_('All summaries on the same date must use the same day schedule.'))
            self.env['baseer.pos.closure']._check_summary(summary)
            domain = [('company_id', '=', summary.company_id.id), ('business_date', '=', summary.business_date), ('id', '!=', summary.id), ('state', '!=', 'cancelled')]
            if summary.period_scope != 'all':
                domain.append(('period_scope', 'in', ['all', summary.period_scope]))
            if self.search_count(domain, limit=1):
                raise ValidationError(_('Use either one All Day summary or separate Morning and Evening summaries for this company and date.'))

    def _validate_draft(self, allow_empty=False):
        for summary in self:
            self._validate_values({'customer_count': summary.customer_count, 'external_reference': summary.external_reference})
            if summary.config_id.company_id != summary.company_id or not summary.config_id.baseer_summary_only:
                raise ValidationError(_('Select the dedicated external-summary POS for this company.'))
            gross = sum((money(line.amount) for line in summary.allocation_ids), ZERO)
            if summary.zero_sales and (gross or summary.customer_count):
                raise ValidationError(_('A no-sales operation must have zero sales and zero customers.'))
            if not allow_empty and not summary.zero_sales and gross <= ZERO:
                raise ValidationError(_('Enter a sales amount, or explicitly declare an operating day with no sales.'))
            if len(summary.allocation_ids) > 25:
                raise ValidationError(_('An external summary supports at most 25 payment methods.'))
            if sum((money(line.amount) for line in summary.allocation_ids), ZERO) > MAX_AMOUNT:
                raise ValidationError(_('The summary total cannot exceed 999999999.99.'))

    @api.model_create_multi
    def create(self, vals_list):
        normalized = []
        for vals in vals_list:
            if self._PROTECTED.intersection(vals):
                raise AccessError(_('Summary status, totals and original-document links are controlled by the server.'))
            self._validate_values(vals)
            company = self.env['res.company'].browse(vals.get('company_id') or self.env.company.id)
            active_company(self, company)
            scope = vals.get('period_scope') or self.env.context.get('default_period_scope', 'all')
            schedule = vals.get('day_schedule') or ('all' if scope == 'all' else 'split')
            normalized.append(dict(vals, company_id=company.id, day_schedule=schedule))
        scoped = clean_context(self, self.env.company)
        with self.env.cr.savepoint():
            records = super(PosSummary, scoped).create(normalized)
            for summary in records:
                summary._serialize_company()
                summary._check_overlap()
                # Persisted ID is an unambiguous stable reference; no extra
                # sequence model or replay token is necessary.
                super(PosSummary, summary).write({'name': 'POS-S/%s/%06d' % (summary.business_date.year, summary.id)})
            records._validate_draft()
            return records

    def write(self, vals):
        if set(vals) == {'is_archived'}:
            lifecycle_manager(self)
            if not isinstance(vals['is_archived'], bool):
                raise ValidationError(_('Archive status must be enabled or disabled.'))
            self._lock()
            return super().write(vals)
        if self._PROTECTED.intersection(vals):
            raise AccessError(_('Summary status, totals and original-document links are controlled by the server.'))
        self._validate_values(vals)
        with self.env.cr.savepoint():
            self._lock()
            self._require_draft()
            if any(summary.replaces_id and any(field in vals and (fields.Date.to_date(vals[field]) if field == 'business_date' else vals[field]) != summary[field]
                    for field in ('business_date', 'period_scope', 'day_schedule')) for summary in self):
                raise ValidationError(_('A replacement must retain the original sales date and shift.'))
            if any('company_id' in vals and vals['company_id'] != row.company_id.id for row in self):
                raise ValidationError(_('The summary company cannot be changed. Create a new summary in the active company.'))
            if any('config_id' in vals and vals['config_id'] != row.config_id.id for row in self):
                raise ValidationError(_('The summary POS configuration cannot be changed.'))
            for summary in self:
                summary._serialize_company()
            result = super().write(vals)
            self._check_overlap()
            self._validate_draft()
            return result

    def unlink(self):
        self._lock('unlink')
        self._require_draft()
        if self.filtered('replaces_id'):
            raise UserError(_('A correction replacement must be retained for review and audit.'))
        return super().unlink()

    def action_archive(self):
        return self.write({'is_archived': True})

    def action_unarchive(self):
        return self.write({'is_archived': False})

    def _business_timestamp(self):
        self.ensure_one()
        # A summary reports a business day, not individual transaction times.
        # 09:00 UTC = Riyadh noon, avoiding UTC-midnight previous-day shifts.
        return datetime.combine(self.business_date, time(9, 0))

    def _check_journal_dates(self):
        self.ensure_one()
        journals = self.config_id.journal_id | self.config_id.invoice_journal_id | self.allocation_ids.payment_method_id.journal_id
        for journal in journals:
            if self.company_id._get_violated_lock_dates(self.business_date, True, journal):
                raise ValidationError(_('The requested business date is locked. Choose an open date; it will not be shifted automatically.'))

    def action_approve(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            self._lock()
            if self.state == 'approved':
                return self._source_action(self, _('External Sales Summary'))
            self._require_draft()
            if not self.env.user.has_group('point_of_sale.group_pos_user') or not self.env.user.has_group('account.group_account_invoice'):
                raise AccessError(_('POS and invoicing permissions are required to approve external summaries.'))
            summary = clean_context(self, self.company_id, authorized_internal=True)
            summary._serialize_company()
            summary._check_overlap()
            summary._validate_draft()
            summary.config_id._validate_baseer_setup()
            summary.allocation_ids._validate_amounts()
            summary._check_journal_dates()
            if summary.zero_sales:
                super(PosSummary, summary).write({'state': 'approved', 'approved_by_id': self.env.user.id, 'approved_at': fields.Datetime.now()})
                return summary._source_action(summary, _('External Sales Summary'))
            if summary.env['pos.session'].search_count([('config_id', '=', summary.config_id.id), ('state', '!=', 'closed')], limit=1):
                raise ValidationError(_('The dedicated summary POS already has an open session. Resolve it before approving another summary.'))
            positive_lines = summary.allocation_ids.filtered(lambda line: line.amount > 0)
            gross = sum((positive_amount(line.amount, summary.env) for line in positive_lines), ZERO)
            quote = native_quote(gross, summary.config_id.baseer_summary_tax_id, summary.config_id.baseer_summary_product_id, summary.company_id)
            timestamp = summary._business_timestamp()
            session = summary.env['pos.session'].create({'config_id': summary.config_id.id, 'baseer_summary_id': summary.id})
            super(PosSummary, summary).write({'session_id': session.id})
            session.set_opening_control(0, _('External report opening; no additional opening cash transaction.'))
            product = summary.config_id.baseer_summary_product_id
            order = summary.env['pos.order'].create({
                'session_id': session.id, 'company_id': summary.company_id.id, 'date_order': timestamp,
                'baseer_summary_id': summary.id, 'source': 'baseer_summary', 'to_invoice': False,
                'amount_total': float(gross), 'amount_tax': float(quote['tax']), 'amount_paid': 0.0, 'amount_return': 0.0,
                'lines': [Command.create({'product_id': product.id, 'qty': 1.0, 'price_unit': float(quote['unit_price']),
                    'price_subtotal': float(quote['net']), 'price_subtotal_incl': float(gross),
                    'discount': 0.0, 'tax_ids': [Command.set(summary.config_id.baseer_summary_tax_id.ids)],
                    'full_product_name': summary.name + (' / ' + summary.external_reference if summary.external_reference else '')})],
            })
            super(PosSummary, summary).write({'order_id': order.id})
            for line in positive_lines:
                payment = summary.env['pos.payment'].create({'pos_order_id': order.id, 'payment_method_id': line.payment_method_id.id,
                    'amount': float(money(line.amount)), 'payment_date': timestamp})
                super(PosSummaryAllocation, line).write({'pos_payment_id': payment.id})
            order.lines._onchange_amount_line_all()
            order._compute_prices()
            if money(order.amount_total) != gross or money(order.amount_tax) != quote['tax'] or money(order.lines.price_subtotal) != quote['net']:
                raise ValidationError(_('The native POS order does not match the summary gross, net and tax amounts.'))
            order.action_pos_order_paid()
            # Native service order flow creates no stock picking.
            order._create_order_picking()
            order._compute_total_cost_in_real_time()
            # Native cashier requests normally use a fresh ORM cache for each
            # payment/count/close RPC. This atomic backend flow uses one cache;
            # explicitly recompute the native cash balance after payments and
            # after the declared count so a stale opening value cannot produce
            # a fictitious closing loss.
            session._compute_cash_balance()
            session.write({'stop_at': timestamp, 'cash_register_balance_end_real': session.cash_register_balance_end})
            session._compute_cash_balance()
            if money(session.cash_register_difference):
                raise ValidationError(_('The native cash count does not match the external summary cash payments.'))
            result = session.action_pos_session_closing_control()
            if result is not True or session.state != 'closed' or order.state != 'done':
                raise ValidationError(_('The native POS session did not close and post successfully.'))
            summary._verify_posting(quote)
            super(PosSummary, summary).write({'state': 'approved', 'approved_by_id': self.env.user.id, 'approved_at': fields.Datetime.now()})
            return summary._source_action(summary, _('External Sales Summary'))

    def _native_moves(self):
        self.ensure_one()
        payments = self.env['account.payment'].search([('pos_session_id', '=', self.session_id.id)]) if self.session_id else self.env['account.payment']
        return self.session_id.move_id | self.session_id.statement_line_ids.move_id | payments.move_id

    def _verify_posting(self, quote):
        self.ensure_one()
        session, order = self.session_id, self.order_id
        moves = self._native_moves()
        moves.write({'baseer_pos_summary_id': self.id})
        if session.order_ids != order or order.picking_ids or order.account_move or not session.move_id or not moves:
            raise ValidationError(_('The summary must produce one native service order and one dedicated session without stock movements or duplicate invoices.'))
        if any(move.state != 'posted' or move.company_id != self.company_id or move.date != self.business_date for move in moves):
            raise ValidationError(_('Every summary accounting entry must be posted in the correct company on the requested business date.'))
        for move in moves:
            if sum((money(line.balance) for line in move.line_ids), ZERO):
                raise ValidationError(_('A native summary accounting entry is not balanced.'))
        income = -sum((money(line.balance) for line in session.move_id.line_ids if line.account_id.account_type in ('income', 'income_other')), ZERO)
        vat = -sum((money(line.balance) for line in session.move_id.line_ids if line.tax_line_id), ZERO)
        if income != quote['net'] or vat != quote['tax'] or money(order.amount_paid) != quote['gross']:
            raise ValidationError(_('Posted native revenue, tax and payments must match the approved summary exactly.'))
        for allocation in self.allocation_ids:
            method = allocation.payment_method_id
            account = method.journal_id.default_account_id if method.type == 'cash' else method.outstanding_account_id
            if sum((money(line.balance) for line in moves.line_ids if line.account_id == account), ZERO) != money(allocation.amount):
                # Several bank methods may legitimately share a liquidity account.
                expected = sum((money(other.amount) for other in self.allocation_ids if
                    (other.payment_method_id.journal_id.default_account_id if other.payment_method_id.type == 'cash' else other.payment_method_id.outstanding_account_id) == account), ZERO)
                if sum((money(line.balance) for line in moves.line_ids if line.account_id == account), ZERO) != expected:
                    raise ValidationError(_('Native cash and clearing balances do not match the declared payment methods.'))

    def _source_action(self, records, title):
        self.ensure_one()
        self.check_access('read')
        records.check_access('read')
        action = {'type': 'ir.actions.act_window', 'name': title, 'res_model': records._name, 'view_mode': 'list,form',
                  'domain': [('id', 'in', records.ids)], 'context': {'allowed_company_ids': self.company_id.ids, 'create': False}}
        if len(records) == 1:
            action.update(view_mode='form', res_id=records.id, views=[(False, 'form')])
        return action

    def action_view_order(self):
        return self._source_action(self.order_id, _('Original POS Order'))

    def action_view_session(self):
        return self._source_action(self.session_id, _('Original POS Session'))

    def action_view_moves(self):
        moves = self._native_moves()
        source = self._source_action(moves, _('Original Accounting Entries'))
        action = self.env['ir.actions.actions']._for_xml_id('account.action_move_journal_line')
        action.update(source)
        action['context'].update(default_move_type='entry', view_no_maturity=True)
        form_view = self.env.ref('account.view_move_form').id
        if len(moves) == 1:
            action.update(view_id=form_view, views=[(form_view, 'form')])
        else:
            list_view = self.env.ref('account.view_move_tree').id
            action.update(view_id=list_view, views=[(list_view, 'list'), (form_view, 'form')])
        return action

    def _get_print_data(self):
        self.ensure_one()
        self.check_access('read')
        currency = self.currency_id
        return {'name': self.name, 'company': self.company_id.name, 'date': str(self.business_date),
                'period': dict(self._fields['period_scope']._description_selection(self.env))[self.period_scope],
                'customers': str(self.customer_count), 'gross': currency.format(self.amount_gross),
                'net': currency.format(self.amount_net), 'tax': currency.format(self.amount_tax),
                'average': currency.format(self.average_per_customer),
                'rows': [{'method': row.payment_method_id.display_name, 'category': row.category_id.name,
                          'amount': currency.format(row.amount)} for row in self.allocation_ids]}

    def action_share_whatsapp(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'approved':
            raise UserError(_('Approve and save the summary before preparing a WhatsApp message.'))
        data = self._get_print_data()
        message = '\n'.join([data['company'], data['name'], data['date'] + ' / ' + data['period'],
            _('Customers: %s', data['customers']), _('Gross sales: %s', data['gross']),
            _('Tax: %s', data['tax']), _('Average per customer: %s', data['average'])]
            + [row['method'] + ': ' + row['amount'] for row in data['rows']])
        return {'type': 'ir.actions.act_url', 'url': 'https://web.whatsapp.com/send?' + urlencode({'text': message}), 'target': 'new'}

    def action_approve_and_share(self):
        self.action_approve()
        return self.action_share_whatsapp()


class PosSummaryAllocation(models.Model):
    _name = 'baseer.pos.summary.allocation'
    _description = 'External POS summary payment allocation'
    _order = 'sequence, id'
    _check_company_auto = True

    summary_id = fields.Many2one('baseer.pos.summary', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='summary_id.company_id', store=True, readonly=True, index=True)
    currency_id = fields.Many2one(related='summary_id.currency_id', readonly=True)
    business_date = fields.Date(related='summary_id.business_date', store=True, readonly=True, index=True)
    period_scope = fields.Selection(related='summary_id.period_scope', store=True, readonly=True)
    state = fields.Selection(related='summary_id.state', store=True, readonly=True, index=True)
    is_current_company = fields.Boolean(related='summary_id.is_current_company', readonly=True)
    sequence = fields.Integer(default=10)
    payment_method_id = fields.Many2one('pos.payment.method', required=True, check_company=True, ondelete='restrict')
    category_id = fields.Many2one(related='payment_method_id.baseer_category_id', store=True, readonly=True)
    amount = fields.Monetary(required=True, currency_field='currency_id')
    pos_payment_id = fields.Many2one('pos.payment', readonly=True, copy=False, ondelete='restrict', check_company=True)
    _summary_method_unique = models.Constraint('unique(summary_id, payment_method_id)', 'Each payment method may appear only once per summary.')
    _payment_unique = models.Constraint('unique(pos_payment_id)', 'A native POS payment may belong to only one allocation.')
    _PROTECTED = {'company_id', 'currency_id', 'is_current_company', 'category_id', 'pos_payment_id', 'business_date', 'period_scope', 'state'}

    def _validate_amounts(self):
        for line in self:
            positive_amount(line.amount, self.env, allow_zero=True)
            line.payment_method_id._validate_baseer_method(line.summary_id.config_id)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if self._PROTECTED.intersection(vals):
                raise AccessError(_('Allocation company, category and native payment links are controlled by the server.'))
            positive_amount(vals.get('amount'), self.env, allow_zero=True)
            if not vals.get('summary_id'):
                raise ValidationError(_('Each payment allocation must belong to a saved draft summary.'))
        summaries = self.env['baseer.pos.summary'].browse(sorted({v['summary_id'] for v in vals_list}))
        with self.env.cr.savepoint():
            summaries._lock()
            summaries._require_draft()
            scoped = clean_context(self, self.env.company)
            records = super(PosSummaryAllocation, scoped).create(vals_list)
            records._validate_amounts()
            summaries._validate_draft(allow_empty=True)
            return records

    def write(self, vals):
        if self._PROTECTED.intersection(vals):
            raise AccessError(_('Allocation company, category and native payment links are controlled by the server.'))
        if 'amount' in vals:
            positive_amount(vals['amount'], self.env, allow_zero=True)
        self.check_access('write')
        with self.env.cr.savepoint():
            self.summary_id._lock()
            self.summary_id._require_draft()
            if 'summary_id' in vals and any(line.summary_id.id != vals['summary_id'] for line in self):
                raise ValidationError(_('Payment allocations cannot be moved between summaries.'))
            result = super().write(vals)
            self._validate_amounts()
            self.summary_id._validate_draft()
            return result

    def unlink(self):
        self.check_access('unlink')
        self.summary_id._lock()
        self.summary_id._require_draft()
        return super().unlink()
