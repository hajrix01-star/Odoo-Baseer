"""HR administrative service; native bill and reconciliation own its finances."""
from odoo import _, api, Command, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import SQL
from odoo.addons.baseer_service_seed.models.catalog import SERVICES
from odoo.addons.baseer_purchase_batch.models.purchase_batch import (
    checked_gross, company_scope, is_principal_purchase_vat, monetary, native_quote,
)


INTERNAL = object()
HR_SERVICES = SERVICES[:15]
SERVICE_TYPES = [(key, english) for key, english, arabic, purpose in HR_SERVICES]
VISA_TYPES = [('issue', 'Issue exit and return'), ('extend', 'Extend exit and return')]
SERVICE_PROVIDERS = {
    'iqama_issue': 'passports', 'iqama_renewal': 'passports', 'visa': 'passports',
    'work_permit_issue': 'hrsd', 'work_permit_renewal': 'hrsd',
    'employee_transfer': 'hrsd', 'profession_change': 'hrsd',
    'health_certificate_issue': 'balady', 'health_certificate_renewal': 'balady',
}


def internal(record):
    return record.env.context.get('baseer_hr_service_internal') is INTERNAL


class EmployeeService(models.Model):
    _name = 'baseer.hr.service'
    _description = 'Employee Service'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'invoice_date desc, id desc'
    _check_company_auto = True
    _bill_unique = models.Constraint('unique(bill_id)', 'A supplier bill can belong to only one employee service.')

    name = fields.Char(default=lambda self: _('New'), required=True, readonly=True, copy=False, index=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, readonly=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    employee_id = fields.Many2one('hr.employee', required=True, check_company=True, ondelete='restrict', index=True, context={'active_test': False}, tracking=True)
    service_type = fields.Selection(SERVICE_TYPES, required=True, default='iqama_renewal', index=True, tracking=True)
    category_map_id = fields.Many2one('baseer.purchase.category.map', compute='_compute_category_map', compute_sudo=False)
    visa_type = fields.Selection(VISA_TYPES)
    partner_id = fields.Many2one('res.partner', string='Service Provider', required=True, check_company=True, ondelete='restrict', index=True, tracking=True)
    service_reference = fields.Char(string='Service Reference', size=240)
    issue_date = fields.Date(string='Service Date', required=True, default=fields.Date.context_today, index=True)
    invoice_date = fields.Date(required=True, default=fields.Date.context_today, index=True)
    expiry_date = fields.Date(index=True)
    gross_amount = fields.Monetary(string='Total Including VAT', required=True, tracking=True)
    vat_enabled = fields.Boolean(string='VAT 15%', default=False)
    tax_id = fields.Many2one('account.tax', readonly=True, check_company=True)
    net_amount = fields.Monetary(compute='_compute_amounts', string='Amount Excluding VAT')
    tax_amount = fields.Monetary(compute='_compute_amounts', string='VAT Amount')
    notes = fields.Text()
    active = fields.Boolean(default=True)
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('cancel', 'Canceled')], default='draft', required=True, readonly=True, copy=False, index=True, tracking=True)
    bill_id = fields.Many2one('account.move', readonly=True, copy=False, check_company=True, ondelete='restrict', index=True)
    bill_name = fields.Char(compute='_compute_bill_summary', compute_sudo=True)
    bill_state = fields.Selection([('draft', 'Draft'), ('posted', 'Posted'), ('cancel', 'Canceled')], compute='_compute_bill_summary', compute_sudo=True, search='_search_bill_state')
    payment_state = fields.Selection(selection=lambda self: self.env['account.move']._fields['payment_state']._description_selection(self.env),
                                     string='Payment Status', compute='_compute_bill_summary', compute_sudo=True, search='_search_payment_state')
    balance = fields.Monetary(compute='_compute_bill_summary', compute_sudo=True, string='Remaining to Pay', search='_search_balance')
    has_posted_refund = fields.Boolean(compute='_compute_bill_summary', compute_sudo=True)
    approved_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)

    _CONTROL = {'name', 'company_id', 'currency_id', 'state', 'bill_id', 'bill_name', 'bill_state', 'payment_state',
                'balance', 'approved_by_id', 'approved_at', 'tax_id', 'net_amount', 'tax_amount', 'category_map_id', 'has_posted_refund'}
    _BUSINESS = {'employee_id', 'service_type', 'visa_type', 'partner_id', 'service_reference', 'issue_date',
                 'invoice_date', 'expiry_date', 'gross_amount', 'vat_enabled', 'notes'}

    def _require_hr(self, manager=False):
        if not self.env.user.has_group('hr.group_hr_manager' if manager else 'hr.group_hr_user'):
            raise AccessError(_('Human Resources access is required for employee services.'))

    def _scoped(self):
        self.ensure_one()
        if self.company_id != self.env.company:
            raise AccessError(_('Switch to the service company before changing this record.'))
        return company_scope(self, self.company_id)

    def _lock(self):
        self._require_hr()
        self.check_access('write')
        for record in self:
            record._scoped()
        if self.ids:
            self.env.cr.execute('SELECT id FROM baseer_hr_service WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(sorted(self.ids))])
            self.env.cr.execute('UPDATE baseer_hr_service SET id=id WHERE id IN %s', [tuple(sorted(self.ids))])
            self.invalidate_recordset()

    @api.depends('company_id', 'service_type')
    def _compute_category_map(self):
        for record in self:
            record.category_map_id = self.env.ref(
                f'baseer_service_seed.mapping_{record.service_type}_company_{record.company_id.id}',
                raise_if_not_found=False,
            ) if record.company_id and record.service_type else False

    @api.onchange('service_type')
    def _onchange_service_type(self):
        for record in self:
            if record.state != 'draft' or record.bill_id:
                continue
            if record.service_type != 'visa':
                record.visa_type = False
            record.partner_id = record._suggested_provider(record.service_type, record.company_id)

    @api.model
    def _suggested_provider(self, service_type, company=None):
        """Resolve the shared seed identity or its legacy company alias."""
        company = company or self.env.company
        empty = self.env['res.partner']
        key = SERVICE_PROVIDERS.get(service_type)
        if not key or company != self.env.company:
            return empty
        partner = self.env.ref(
            f'baseer_service_seed.provider_{key}', raise_if_not_found=False,
        )
        if not partner:
            partner = self.env.ref(
                f'baseer_service_seed.provider_{key}_company_{company.id}', raise_if_not_found=False,
            )
        if not partner or partner._name != 'res.partner' or not partner.exists():
            return empty
        try:
            partner.check_access('read')
        except AccessError:
            return empty
        if (not partner.active or partner.supplier_rank <= 0
                or any(record.company_id and record.company_id != company
                       for record in partner | partner.commercial_partner_id)):
            return empty
        return partner

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if 'partner_id' in fields_list and 'default_partner_id' not in self.env.context:
            service_type = values.get('service_type', self.env.context.get('default_service_type', 'iqama_renewal'))
            values['partner_id'] = self._suggested_provider(service_type).id
        return values

    def _configured_tax(self, enabled, company):
        tax = company.account_purchase_tax_id if enabled else self.env['account.tax']
        if enabled and not is_principal_purchase_vat(tax, company):
            raise ValidationError(_('Configure the principal 15% purchase VAT for this company first.'))
        return tax

    @api.onchange('vat_enabled')
    def _onchange_vat_enabled(self):
        for record in self:
            record.tax_id = record._configured_tax(record.vat_enabled, record.company_id)

    @api.depends('gross_amount', 'tax_id', 'category_map_id', 'partner_id', 'bill_id')
    def _compute_amounts(self):
        for record in self:
            if record.bill_id:
                bill = record._summary_bill()
                record.net_amount, record.tax_amount = bill.amount_untaxed, bill.amount_tax
            elif record.gross_amount and record.category_map_id and record.partner_id:
                # An unsaved form can contain intermediate amounts while the user
                # edits it. Persistence and approval retain strict validation.
                try:
                    gross = checked_gross(record.gross_amount, record.env)
                except ValidationError:
                    record.net_amount, record.tax_amount = 0, 0
                    continue
                quote = native_quote(gross, record.tax_id,
                                     record.category_map_id.product_id, record.partner_id, record.company_id)
                record.net_amount, record.tax_amount = float(quote['net']), float(quote['tax'])
            else:
                record.net_amount, record.tax_amount = record.gross_amount or 0, 0

    def _summary_bill(self):
        self.ensure_one()
        # Narrow permitted projection: this already-linked bill only, no finance writes.
        bill = self.bill_id.sudo()
        if bill and bill.company_id != self.company_id:
            raise AccessError(_('The linked service bill belongs to another company.'))
        return bill

    @api.depends('bill_id', 'bill_id.name', 'bill_id.state', 'bill_id.payment_state', 'bill_id.amount_residual', 'bill_id.reversal_move_ids.state')
    def _compute_bill_summary(self):
        for record in self:
            bill = record._summary_bill()
            record.bill_name = bill.name if bill else False
            record.bill_state = bill.state if bill else False
            record.payment_state = bill.payment_state if bill else False
            record.balance = bill.amount_residual if bill else 0
            record.has_posted_refund = bool(bill.reversal_move_ids.filtered(lambda move: move.state == 'posted')) if bill else False

    def _search_bill_projection(self, projection, bill_field, operator, value):
        # Apply the caller's service ACL and company rules before the narrow
        # read-only invoice projection. Never traverse account.move through an
        # HR user's domain, or materialize the company's full history in Python.
        query = self._search([('bill_id', '!=', False)])
        bill_alias = query.join(self._table, 'bill_id', 'account_move', 'id', 'service_summary')
        query.add_where(SQL('%s = %s', SQL.identifier(self._table, 'company_id'),
                            SQL.identifier(bill_alias, 'company_id')))
        bill_model = self.env['account.move'].sudo()
        condition = Domain(bill_field, operator, value).optimize_full(bill_model)
        query.add_where(condition._to_sql(bill_model, bill_alias, query))
        result = Domain('id', 'in', query)
        # The projection is False (states), or zero (balance), without a bill.
        # Use Odoo's in-memory operator semantics on one unsaved value; no
        # accounting record is created or exposed by this check.
        empty = self.new({projection: 0 if projection == 'balance' else False, 'bill_id': False})
        if Domain(projection, operator, value)._as_predicate(empty)(empty):
            result |= Domain('bill_id', '=', False)
        return result

    def _search_bill_state(self, operator, value):
        return self._search_bill_projection('bill_state', 'state', operator, value)

    def _search_payment_state(self, operator, value):
        return self._search_bill_projection('payment_state', 'payment_state', operator, value)

    def _search_balance(self, operator, value):
        return self._search_bill_projection('balance', 'amount_residual', operator, value)

    def _validate_inputs(self):
        for record in self:
            record = record._scoped()
            record.employee_id.with_context(active_test=False).check_access('read')
            record.partner_id.check_access('read')
            if record.employee_id.company_id != record.company_id:
                raise ValidationError(_('Choose an employee from the service company.'))
            partner = record.partner_id
            if (not partner.active
                    or any(party.company_id and party.company_id != record.company_id
                           for party in partner | partner.commercial_partner_id)):
                raise ValidationError(_('Choose an active shared provider or one from the service company.'))
            mapping = record.category_map_id
            if (not mapping or mapping.company_id != record.company_id or not mapping.active
                    or record.service_type not in dict(SERVICE_TYPES)):
                raise ValidationError(_('Prepare the active employee-service category mapping for this company.'))
            mapping._validated_expense_account()
            if record.service_type == 'visa' and record.visa_type not in dict(VISA_TYPES):
                raise ValidationError(_('Choose whether to issue or extend the exit-and-return visa.'))
            if record.service_type != 'visa' and record.visa_type:
                raise ValidationError(_('Visa details apply only to the Visas service.'))
            if record.service_type == 'other_employee' and not (record.notes or '').strip():
                raise ValidationError(_('Describe the other employee service in the notes.'))
            if record.expiry_date and record.expiry_date < record.issue_date:
                raise ValidationError(_('The expiry date cannot be before the service date.'))
            if record.tax_id != record._configured_tax(record.vat_enabled, record.company_id):
                raise ValidationError(_('The purchase tax changed. Review the VAT switch before approval.'))
            native_quote(checked_gross(record.gross_amount, record.env), record.tax_id,
                         mapping.product_id, partner, record.company_id)

    @api.model_create_multi
    def create(self, vals_list):
        self._require_hr()
        clean = []
        for incoming in vals_list:
            values = dict(incoming)
            forbidden = (self._CONTROL - {'company_id'}) & values.keys()
            if forbidden:
                raise AccessError(_('Service accounting links and control fields are managed by approval.'))
            if values.get('company_id', self.env.company.id) != self.env.company.id:
                raise AccessError(_('Create the service in the active company.'))
            checked_gross(values.get('gross_amount'), self.env)
            if 'partner_id' not in values:
                service_type = values.get('service_type', self.default_get(['service_type']).get('service_type'))
                # An explicit caller default, including False, remains a manual choice.
                values['partner_id'] = (self.env.context['default_partner_id']
                                        if 'default_partner_id' in self.env.context
                                        else self._suggested_provider(service_type).id)
            values.update(company_id=self.env.company.id,
                          tax_id=self._configured_tax(values.get('vat_enabled', False), self.env.company).id,
                          name=self.env['ir.sequence'].next_by_code('baseer.hr.service') or _('New'),
                          state='draft', bill_id=False)
            clean.append(values)
        with self.env.cr.savepoint():
            records = super(EmployeeService, company_scope(self, self.env.company)).create(clean)
            records._validate_inputs()
            return records

    def write(self, values):
        if internal(self):
            return super().write(values)
        self._lock()
        if self._CONTROL & values.keys():
            raise AccessError(_('Service accounting links and control fields cannot be changed manually.'))
        if self._BUSINESS & values.keys() and self.filtered(lambda record: record.bill_id or record.state != 'draft'):
            raise UserError(_('An issued or canceled service is read-only. Correct its bill with a native reversal.'))
        if 'gross_amount' in values:
            checked_gross(values['gross_amount'], self.env)
        with self.env.cr.savepoint():
            for record in self:
                changed = dict(values)
                if 'service_type' in changed and changed['service_type'] != record.service_type:
                    if 'partner_id' not in changed:
                        changed['partner_id'] = record._suggested_provider(changed['service_type'], record.company_id).id
                    if changed['service_type'] != 'visa' and 'visa_type' not in changed:
                        changed['visa_type'] = False
                if 'vat_enabled' in changed:
                    changed['tax_id'] = record._configured_tax(changed['vat_enabled'], record.company_id).id
                super(EmployeeService, record).write(changed)
                if record.state == 'draft' and not record.bill_id:
                    record._validate_inputs()
        return True

    def unlink(self):
        self._lock()
        if self.filtered(lambda record: record.bill_id or record.state == 'approved'):
            raise UserError(_('Issued employee-service history cannot be deleted. Archive the record instead.'))
        return super().unlink()

    def _service_bill_description(self):
        self.ensure_one()
        label = dict(self._fields['service_type']._description_selection(self.env))[self.service_type]
        parts = [label, self.employee_id.name, self.name]
        if self.visa_type:
            parts.append(dict(self._fields['visa_type']._description_selection(self.env))[self.visa_type])
        if self.service_reference:
            parts.append(self.service_reference)
        return ' / '.join(parts)

    def action_approve(self):
        self.ensure_one()
        self._require_hr(manager=True)
        if not self.env.user.has_group('account.group_account_invoice'):
            raise AccessError(_('Native accounting access is required to approve the supplier bill.'))
        self._lock()
        if self.bill_id:
            return self.action_view_bill()
        if self.state != 'draft':
            raise UserError(_('Only a draft employee service can be approved.'))
        record = self._scoped()
        record._validate_inputs()
        mapping, company = record.category_map_id, record.company_id
        account = mapping._validated_expense_account()
        quote = native_quote(checked_gross(record.gross_amount, record.env), record.tax_id,
                             mapping.product_id, record.partner_id, company)
        journal = record.env['account.journal'].search([('company_id', '=', company.id), ('type', '=', 'purchase'), ('active', '=', True)], order='sequence,id', limit=1)
        if not journal or journal.currency_id and journal.currency_id != company.currency_id:
            raise ValidationError(_('Configure an active purchase journal in the company currency.'))
        if company._get_violated_lock_dates(record.invoice_date, bool(record.tax_id), journal):
            raise ValidationError(_('The invoice date is in a locked accounting period.'))
        with self.env.cr.savepoint():
            bill = record.env['account.move'].with_context(baseer_hr_service_internal=INTERNAL).create({
                'move_type': 'in_invoice', 'company_id': company.id, 'currency_id': company.currency_id.id,
                'journal_id': journal.id, 'partner_id': record.partner_id.id,
                'invoice_date': record.invoice_date, 'date': record.invoice_date,
                'invoice_date_due': record.invoice_date, 'invoice_payment_term_id': False,
                'fiscal_position_id': False, 'baseer_hr_service_id': record.id, 'ref': record.name,
                'invoice_line_ids': [Command.create({
                    'name': record._service_bill_description(), 'product_id': mapping.product_id.id,
                    'account_id': account.id, 'quantity': 1, 'price_unit': float(quote['unit_price']),
                    'discount': 0, 'tax_ids': [Command.set(record.tax_id.ids)],
                })],
            })
            bill.action_post()
            if (bill.state != 'posted' or bill.date != record.invoice_date or bill.invoice_date != record.invoice_date
                    or monetary(bill.amount_total) != quote['gross']
                    or monetary(bill.amount_untaxed) != quote['net'] or monetary(bill.amount_tax) != quote['tax']):
                raise ValidationError(_('The native bill does not match the reviewed service amount, VAT or date.'))
            record.with_context(baseer_hr_service_internal=INTERNAL).write({
                'state': 'approved', 'bill_id': bill.id,
                'approved_by_id': record.env.uid, 'approved_at': fields.Datetime.now(),
            })
        return record.action_view_bill()

    def action_view_bill(self):
        self.ensure_one()
        self.check_access('read')
        if not self.bill_id:
            raise UserError(_('No supplier bill has been issued for this service.'))
        self.bill_id.check_access('read')
        return {'type': 'ir.actions.act_window', 'name': _('Supplier Bill'), 'res_model': 'account.move',
                'res_id': self.bill_id.id, 'view_mode': 'form',
                'views': [(self.env.ref('account.view_move_form').id, 'form')]}

    def action_register_payment(self):
        self.ensure_one()
        self._lock()
        bill = self._scoped().bill_id
        bill.check_access('write')
        if not bill or bill.state != 'posted' or bill.payment_state in ('paid', 'reversed'):
            raise UserError(_('This service has no posted supplier bill awaiting payment.'))
        return bill.action_register_payment()

    def action_cancel(self):
        self._lock()
        for record in self:
            if record.bill_id and record.bill_state != 'cancel' and record.payment_state != 'reversed':
                raise UserError(_('Reverse or cancel the supplier bill in Accounting before canceling this service.'))
            record.with_context(baseer_hr_service_internal=INTERNAL).write({'state': 'cancel'})
        return True

    def action_reset_draft(self):
        self._lock()
        if self.filtered('bill_id'):
            raise UserError(_('An issued service cannot be reopened. Create a new service after the reviewed correction.'))
        return self.with_context(baseer_hr_service_internal=INTERNAL).write({'state': 'draft'})

    def action_renew(self):
        self.ensure_one()
        self._require_hr()
        self.check_access('read')
        record = self._scoped()
        return {'type': 'ir.actions.act_window', 'name': _('New Employee Service'), 'res_model': self._name,
                'view_mode': 'form', 'context': {
                    'default_employee_id': record.employee_id.id, 'default_partner_id': record.partner_id.id,
                    'default_service_type': record.service_type, 'default_visa_type': record.visa_type,
                    'default_service_reference': record.service_reference,
                    'default_issue_date': fields.Date.to_string(fields.Date.context_today(record)),
                    'default_invoice_date': fields.Date.to_string(fields.Date.context_today(record)),
                    'default_expiry_date': False,
                }}

    def action_print(self):
        self.check_access('read')
        return self.env.ref('baseer_hr_services.action_report_baseer_hr_service').report_action(self)

    def _report_data(self):
        self.ensure_one()
        self.check_access('read')
        date = lambda value: fields.Date.to_string(value) if value else ''
        money = lambda value: f'{monetary(value):,.2f}'
        selection = lambda field, value: dict(self._fields[field]._description_selection(self.env)).get(value, '')
        return {
            'name': self.name, 'company_name': self.company_id.name, 'company_logo': self.company_id.logo,
            'employee_name': self.employee_id.name, 'service_label': selection('service_type', self.service_type),
            'visa_label': selection('visa_type', self.visa_type), 'provider_name': self.partner_id.name,
            'reference': self.service_reference or '', 'issue_date': date(self.issue_date),
            'invoice_date': date(self.invoice_date), 'expiry_date': date(self.expiry_date),
            'gross': money(self.gross_amount), 'net': money(self.net_amount), 'tax': money(self.tax_amount),
            'balance': money(self.balance), 'currency': self.currency_id.name,
            'bill_name': self.bill_name or '', 'state_label': selection('state', self.state),
            'payment_label': selection('payment_state', self.payment_state),
        }
