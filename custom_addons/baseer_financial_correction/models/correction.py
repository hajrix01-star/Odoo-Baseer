"""One-document corrections; native accounting owns every financial calculation."""
import json
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from odoo import Command, api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from .dispatcher import reject_hr_accounts


CORRECTION_CAPABILITY = object()
_CAP_KEY = '_baseer_financial_correction_capability'
_CENT = Decimal('0.01')


def _number(value, label, minimum=Decimal('0'), maximum=Decimal('999999999.99')):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(_('%s must be entered as a number.', label))
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise ValidationError(_('%s must be a finite decimal number.', label)) from None
    if not number.is_finite() or number < minimum or number > maximum or number != number.quantize(_CENT):
        raise ValidationError(_('%s must be within the allowed range with at most two decimal places.', label))
    return number


def _money(value):
    return Decimal(str(value or 0)).quantize(_CENT)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':'))


class FinancialCorrection(models.TransientModel):
    _name = 'baseer.financial.correction'
    _description = 'Correct Financial Operation'

    company_id = fields.Many2one('res.company', required=True, readonly=True)
    move_id = fields.Many2one('account.move', readonly=True)
    payment_id = fields.Many2one('account.payment', readonly=True)
    batch_line_id = fields.Many2one('baseer.purchase.batch.line', readonly=True)
    operation = fields.Selection([('edit', 'Edit'), ('cancel', 'Cancel operation')],
                                 default='edit', required=True, readonly=True)
    payment_cancel_ack = fields.Boolean(string='I confirm this recorded payment must be cancelled')
    partner_id = fields.Many2one('res.partner', required=True)
    reason = fields.Text()
    correct_payment = fields.Boolean(string='Correct recorded payment')
    payment_amount_input = fields.Char(string='Actual payment amount')
    payment_journal_id = fields.Many2one('account.journal', string='Payment journal')
    payment_method_line_id = fields.Many2one('account.payment.method.line', string='Payment method')
    gross_amount_input = fields.Char(string='Invoice total including tax')
    line_ids = fields.One2many('baseer.financial.correction.line', 'wizard_id', string='Invoice lines')
    baseline_json = fields.Text(readonly=True)
    operation_token = fields.Char(readonly=True)
    completed = fields.Boolean(readonly=True)
    audit_id = fields.Many2one('baseer.financial.correction.audit', readonly=True)

    _PROTECTED = {'company_id', 'move_id', 'payment_id', 'batch_line_id', 'baseline_json',
                  'operation_token', 'completed', 'audit_id', 'create_uid', 'operation'}

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_('Open financial corrections from the source operation.'))

    def write(self, vals):
        if self._PROTECTED.intersection(vals):
            raise AccessError(_('Correction source and audit fields are managed by the system.'))
        self._require_owner()
        self._lock_wizard_rows()
        if any(self.mapped('completed')):
            raise UserError(_('This correction has already been completed.'))
        return super().write(vals)

    def _require_owner(self):
        self.check_access('read')
        for wizard in self:
            if wizard.create_uid.id != self.env.uid:
                raise AccessError(_('Only the user who opened this correction can confirm it.'))
            source = wizard.move_id or wizard.payment_id
            source._baseer_correction_require_access()

    def _clean_context(self, company):
        return {'allowed_company_ids': company.ids, 'lang': self.env.user.lang,
                'tz': self.env.user.tz}

    @api.onchange('payment_journal_id')
    def _onchange_payment_journal_id(self):
        for wizard in self:
            journal = wizard.payment_journal_id
            methods = (journal.inbound_payment_method_line_ids if wizard.payment_id.payment_type == 'inbound'
                       else journal.outbound_payment_method_line_ids)
            eligible = methods.filtered(lambda method: method.code == 'manual'
                and method.payment_account_id == journal.default_account_id
                and journal.default_account_id.account_type == 'asset_cash')
            wizard.payment_method_line_id = eligible if len(eligible) == 1 else False

    def _lock_wizard_rows(self):
        self.flush_recordset()
        if self:
            self.env.cr.execute('SELECT id FROM baseer_financial_correction WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(self.ids)])
            self.invalidate_recordset(['completed'])

    @api.model
    def _open_for_source(self, move=False, payment=False, batch_line=False, operation='edit'):
        if operation not in ('edit', 'cancel'):
            raise ValidationError(_('Unknown financial operation.'))
        move = move or self.env['account.move']
        payment = payment or self.env['account.payment']
        batch_line = batch_line or self.env['baseer.purchase.batch.line']
        if batch_line:
            batch_line.ensure_one()
            batch_line.check_access('read')
            if batch_line.baseer_cancelled:
                raise UserError(_('This purchase row is already cancelled.'))
            if batch_line.batch_id.state != 'approved':
                raise UserError(_('Correct draft batch rows directly before approval.'))
            move, payment = batch_line.move_id, batch_line.payment_id
        source = move or payment
        source.ensure_one()
        source._baseer_correction_require_access()
        company = source.company_id
        context = self._clean_context(company)
        model = self.with_context(context)
        move, payment, batch_line = (record.with_context(context) for record in (move, payment, batch_line))
        # Resolve the entire existing settlement, never trust the entry button's hint.
        move, payment = model._resolve_pair(move, payment)
        if not batch_line:
            domain = [('move_id', '=', move.id)] if move else [('payment_id', '=', payment.id)]
            found = model.env['baseer.purchase.batch.line'].search(domain, limit=2)
            if len(found) > 1:
                raise UserError(_('This operation is shared by several batch rows.'))
            batch_line = found
        model._validate_source(move, payment, batch_line, operation=operation)
        vals = {'company_id': company.id, 'move_id': move.id, 'payment_id': payment.id,
                'batch_line_id': batch_line.id, 'partner_id': source.partner_id.id,
                'reason': '', 'operation': operation, 'correct_payment': bool(payment and not move),
                'payment_amount_input': str(_money(payment.amount)) if payment else False,
                'payment_journal_id': payment.journal_id.id,
                'payment_method_line_id': payment.payment_method_line_id.id,
                'gross_amount_input': str(_money(move.amount_total)) if batch_line else False,
                'operation_token': str(uuid4()),
                'baseline_json': model._snapshot(move, payment, batch_line),
                'line_ids': [Command.create({'original_line_id': line.id, 'name': line.name,
                    'quantity_input': str(line.quantity), 'price_unit_input': str(line.price_unit),
                    'discount_input': str(line.discount), 'tax_ids': [Command.set(line.tax_ids.ids)]})
                    for line in move.invoice_line_ids.filtered(lambda l: l.display_type == 'product')]
                    if not batch_line and operation == 'edit' else []}
        wizard = super(FinancialCorrection, model.with_context(**{_CAP_KEY: CORRECTION_CAPABILITY})).create(vals)
        return {'type': 'ir.actions.act_window', 'name': _('Cancel operation') if operation == 'cancel' else _('Correct operation'),
                'res_model': self._name, 'res_id': wizard.id, 'view_mode': 'form',
                'target': 'new', 'context': {**context, 'edit': True, 'form_view_initial_mode': 'edit'}}

    @api.model
    def _resolve_pair(self, move, payment):
        if payment:
            counterpart = payment._seek_for_lines()[1]
            partials = counterpart.matched_debit_ids | counterpart.matched_credit_ids
            linked = payment.invoice_ids | ((partials.debit_move_id.move_id | partials.credit_move_id.move_id) - payment.move_id)
            if len(linked) > 1 or any(not item.is_invoice() for item in linked):
                raise UserError(_('Shared or complex payments must use their native reconciliation workflow.'))
            if move and linked and move != linked:
                raise UserError(_('The payment is associated with another invoice.'))
            move = move or linked
        if move:
            terms = move.line_ids.filtered(lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable'))
            partials = terms.matched_debit_ids | terms.matched_credit_ids
            others = (partials.debit_move_id.move_id | partials.credit_move_id.move_id) - move
            reconciled_payments = others.origin_payment_id
            linked_payments = move.matched_payment_ids | reconciled_payments
            if len(linked_payments) > 1 or others != reconciled_payments.move_id:
                raise UserError(_('Only an invoice with one dedicated manual payment can be corrected here.'))
            if payment and linked_payments and payment != linked_payments:
                raise UserError(_('The payment link no longer matches the invoice.'))
            payment = payment or linked_payments
        return move, payment

    @api.model
    def _validate_source(self, move, payment, batch_line, operation='edit'):
        resolved_move, resolved_payment = self._resolve_pair(move, payment)
        if resolved_move != move or resolved_payment != payment:
            raise UserError(_('The invoice and payment association changed. Open a fresh operation.'))
        company = (move or payment).company_id
        if company.id != self.env.company.id or company not in self.env.user.company_ids:
            raise AccessError(_('Select the source company before correcting this operation.'))
        if company.currency_id.name != 'SAR' or company.currency_id.decimal_places != 2:
            raise UserError(_('This correction workflow requires a SAR company with two decimal places.'))
        if operation == 'cancel' and all(record.state == 'cancel' for record in move | payment.move_id):
            raise UserError(_('This operation is already cancelled.'))
        if operation == 'edit' and move and move.state == 'cancel':
            raise UserError(_('A cancelled invoice cannot be edited here. Complete its cancellation or create a new source document.'))
        for record in move | payment.move_id:
            record.check_access('write')
            if record.company_id != company or record.currency_id != company.currency_id:
                raise UserError(_('Foreign-currency and cross-company corrections require the native workflow.'))
            allowed_states = ('posted', 'cancel') if operation == 'cancel' else ('posted',)
            if record.state not in allowed_states or record.date > fields.Date.context_today(self) or record.auto_post != 'no':
                raise UserError(_('Only posted operations in the current or an earlier open period are supported.'))
            if record.inalterable_hash or record.need_cancel_request:
                raise UserError(_('This operation is secured or requires an electronic cancellation.'))
            if record._get_violated_lock_dates(record.date, record._affect_tax_report()):
                raise UserError(_('The accounting or tax period is locked.'))
            if record.tax_cash_basis_rec_id or record.tax_cash_basis_origin_move_id:
                raise UserError(_('Cash-basis tax entries require their native correction workflow.'))
            if record.reversed_entry_id or record.reversal_move_ids:
                raise UserError(_('Reversed operations and credit notes require their native workflow.'))
            if 'edi_document_ids' in record._fields and record.edi_document_ids:
                raise UserError(_('Electronic documents must use the native cancellation or credit-note workflow.'))
        if move:
            move._baseer_correction_require_access()
            move._baseer_assert_correction_eligible()
            if move.move_type not in ('in_invoice', 'out_invoice') or not 1 <= len(move.invoice_line_ids) <= 50:
                raise UserError(_('Only ordinary customer or supplier invoices with at most 50 lines are supported.'))
            if move.invoice_cash_rounding_id:
                raise UserError(_('Invoices with cash rounding require the native correction workflow.'))
            if any(t.tax_exigibility == 'on_payment' for t in move.invoice_line_ids.tax_ids.flatten_taxes_hierarchy()):
                raise UserError(_('Cash-basis taxes require the native correction workflow.'))
        if payment:
            payment._baseer_correction_require_access()
            payment._baseer_assert_correction_eligible()
            liquidity, counterpart, writeoffs = payment._seek_for_lines()
            allowed_payment_states = ('paid', 'in_process', 'canceled') if operation == 'cancel' else ('paid', 'in_process')
            if (payment.company_id != company or payment.currency_id != company.currency_id
                    or payment.state not in allowed_payment_states or len(liquidity) != 1
                    or len(counterpart) != 1 or writeoffs or not payment.move_id):
                raise UserError(_('Only a simple posted manual payment is supported.'))
            if payment.reconciled_statement_line_ids or liquidity.matched_debit_ids or liquidity.matched_credit_ids:
                raise UserError(_('Payments matched to bank statements cannot be corrected here.'))
            self._validate_method(payment.payment_method_line_id, company, payment.payment_type)
            if liquidity.account_id != payment.journal_id.default_account_id:
                raise UserError(_('Payments through clearing accounts require their native workflow.'))
            # Every partial must connect only this invoice and this payment.
            partials = counterpart.matched_debit_ids | counterpart.matched_credit_ids
            allowed = move | payment.move_id
            if ((partials.debit_move_id.move_id | partials.credit_move_id.move_id) - allowed
                    or partials.exchange_move_id
                    or self.env['account.move'].search_count([('tax_cash_basis_rec_id', 'in', partials.ids)], limit=1)):
                raise UserError(_('Shared settlements, exchange differences and cash-basis settlements are excluded.'))
            if partials.full_reconcile_id:
                full_lines = partials.full_reconcile_id.reconciled_line_ids
                if full_lines.move_id - allowed:
                    raise UserError(_('The reconciliation includes another operation.'))
            if move and payment.partner_id.commercial_partner_id != move.partner_id.commercial_partner_id:
                raise UserError(_('The current invoice and payment counterparties do not agree.'))
            if payment.invoice_ids - move:
                raise UserError(_('The payment is associated with another invoice.'))
        if batch_line:
            batch_line.check_access('read')
            if batch_line.baseer_cancelled:
                raise UserError(_('This purchase row is already cancelled.'))
            if (batch_line.company_id != company or batch_line.batch_id.state != 'approved'
                    or batch_line.move_id != move or batch_line.payment_id != payment):
                raise UserError(_('The approved batch source no longer matches this operation.'))

    @api.model
    def _validate_method(self, method, company, payment_type):
        if len(method) != 1:
            raise ValidationError(_('Select one manual payment method.'))
        method.check_access('read')
        journal = method.journal_id
        journal.check_access('read')
        reject_hr_accounts(journal, journal.default_account_id | method.payment_account_id)
        if (journal.company_id != company or not journal.active or journal.type not in ('cash', 'bank')
                or method.payment_type != payment_type or method.code != 'manual'
                or (journal.currency_id and journal.currency_id != company.currency_id)
                or method.payment_account_id != journal.default_account_id
                or journal.default_account_id.account_type != 'asset_cash'):
            raise UserError(_('Choose a manual payment method linked directly to this company’s cash or bank account.'))

    @api.model
    def _snapshot(self, move, payment, batch_line):
        # Capture the persisted native state, including recomputations queued by
        # the source form, rather than a cached pre-flush write timestamp.
        self.env.flush_all()
        moves = move | payment.move_id
        moves.invalidate_recordset()
        payment.invalidate_recordset()
        batch_line.invalidate_recordset()
        batch_line.batch_id.invalidate_recordset()
        moves.line_ids.invalidate_recordset()
        partials = moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids
        return _json({'moves': [{'id': record.id, 'name': record.name, 'write_date': record.write_date,
            'state': record.state, 'date': record.date, 'invoice_date': record.invoice_date,
            'partner': record.partner_id.id, 'journal': record.journal_id.id, 'ref': record.ref,
            'total': record.amount_total, 'residual': record.amount_residual,
            'matched_payment_ids': record.matched_payment_ids.ids,
            'lines': [{'id': line.id, 'write_date': line.write_date, 'account': line.account_id.id,
                'partner': line.partner_id.id, 'balance': line.balance, 'amount_currency': line.amount_currency,
                'name': line.name, 'quantity': line.quantity, 'price': line.price_unit,
                'discount': line.discount, 'taxes': [{'id': tax.id, 'write_date': tax.write_date,
                    'amount': tax.amount, 'amount_type': tax.amount_type, 'price_include': tax.price_include,
                    'include_base_amount': tax.include_base_amount, 'is_base_affected': tax.is_base_affected,
                    'tax_exigibility': tax.tax_exigibility, 'active': tax.active, 'sequence': tax.sequence,
                    'children': tax.children_tax_ids.ids,
                    'repartition': [(part.id, part.factor_percent, part.account_id.id, part.repartition_type, part.tag_ids.ids)
                                    for part in tax.invoice_repartition_line_ids]}
                    for tax in (line.tax_ids | line.tax_ids.flatten_taxes_hierarchy()).sorted('id')]}
                for line in record.line_ids.sorted('id')]} for record in moves.sorted('id')],
            'payment': {'id': payment.id, 'name': payment.name, 'date': payment.date,
                'write_date': payment.write_date, 'amount': payment.amount,
                'partner': payment.partner_id.id, 'journal': payment.journal_id.id,
                'method': payment.payment_method_line_id.id, 'state': payment.state,
                'invoice_ids': payment.invoice_ids.ids} if payment else False,
            'batch': {'id': batch_line.id, 'write_date': batch_line.write_date,
                'batch_write_date': batch_line.batch_id.write_date, 'gross': batch_line.gross_amount,
                'partner_id': batch_line.partner_id.id, 'tax_id': batch_line.tax_id.id,
                'category_map_id': batch_line.category_map_id.id,
                'payment_method_line_id': batch_line.payment_method_line_id.id,
                'is_credit': batch_line.is_credit, 'invoice_date': batch_line.invoice_date,
                'supplier_ref': batch_line.supplier_ref, 'move_id': batch_line.move_id.id,
                'payment_id': batch_line.payment_id.id,
                'cancelled': batch_line.baseer_cancelled,
                'cancel_reason': batch_line.baseer_cancel_reason,
                'cancelled_by': batch_line.baseer_cancelled_by_id.id,
                'cancelled_at': batch_line.baseer_cancelled_at} if batch_line else False,
            'partials': [{'id': p.id, 'debit': p.debit_move_id.id, 'credit': p.credit_move_id.id,
                'amount': p.amount, 'write_date': p.write_date} for p in partials.sorted('id')]})

    def _lock(self):
        self.ensure_one()
        self.env.flush_all()
        self.env.cr.execute('SELECT id FROM baseer_financial_correction WHERE id=%s FOR UPDATE', [self.id])
        self.invalidate_recordset()
        if self.batch_line_id:
            self.batch_line_id.batch_id._lock_batches()
        moves = self.move_id | self.payment_id.move_id
        for table, records in [('account_move', moves), ('account_payment', self.payment_id),
                               ('account_move_line', moves.line_ids)]:
            if records:
                self.env.cr.execute('SELECT id FROM ' + table + ' WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(records.ids)])
        partials = moves.line_ids.matched_debit_ids | moves.line_ids.matched_credit_ids
        if partials:
            self.env.cr.execute('SELECT id FROM account_partial_reconcile WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(partials.ids)])
        self.env.invalidate_all()

    def _validate_reason(self):
        self.ensure_one()
        if not self.reason or not self.reason.strip() or len(self.reason.strip()) > 2000:
            raise ValidationError(_('Enter a correction reason of at most 2,000 characters.'))

    def _input_values(self):
        self.ensure_one()
        self._validate_reason()
        self.partner_id.check_access('read')
        if not self.partner_id.active or any(p.company_id and p.company_id != self.company_id
                for p in self.partner_id | self.partner_id.commercial_partner_id):
            raise ValidationError(_('Choose an active counterparty available to this company.'))
        partner = self.partner_id.with_company(self.company_id)
        reject_hr_accounts(self, partner.property_account_payable_id | partner.property_account_receivable_id)
        payment_vals = False
        if self.correct_payment:
            if not self.payment_id:
                raise UserError(_('Register a new payment from the invoice using the native payment action.'))
            amount = _number(self.payment_amount_input, _('Payment amount'), minimum=_CENT)
            self._validate_method(self.payment_method_line_id, self.company_id, self.payment_id.payment_type)
            if self.payment_journal_id != self.payment_method_line_id.journal_id:
                raise ValidationError(_('The payment method must belong to the selected journal.'))
            payment_vals = {'amount': float(amount), 'partner_id': self.partner_id.id,
                'journal_id': self.payment_journal_id.id, 'payment_method_line_id': self.payment_method_line_id.id}
        elif self.payment_id and self.partner_id.commercial_partner_id != self.payment_id.partner_id.commercial_partner_id:
            raise UserError(_('Changing the counterparty also requires explicit confirmation of the payment correction.'))
        move_vals = False
        if not self.move_id and not self.correct_payment:
            raise ValidationError(_('Select the payment correction to correct a standalone payment.'))
        if self.move_id:
            if self.batch_line_id:
                _number(self.gross_amount_input, _('Invoice total'), minimum=_CENT)
                move_vals = self.batch_line_id._baseer_prepare_correction_values(self)
            else:
                originals = self.move_id.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
                if len(self.line_ids) != len(originals) or self.line_ids.original_line_id != originals:
                    raise ValidationError(_('The correction must contain exactly the original invoice lines.'))
                commands = []
                for line in self.line_ids:
                    quantity = _number(line.quantity_input, _('Quantity'), minimum=_CENT)
                    price = _number(line.price_unit_input, _('Unit price'))
                    discount = _number(line.discount_input, _('Discount'), maximum=Decimal('100'))
                    line.tax_ids.check_access('read')
                    reject_hr_accounts(self, (line.tax_ids | line.tax_ids.flatten_taxes_hierarchy()).invoice_repartition_line_ids.account_id)
                    tax_use = 'purchase' if self.move_id.move_type == 'in_invoice' else 'sale'
                    if (any(t.type_tax_use != tax_use for t in line.tax_ids)
                            or any(not t.active or t.company_id != self.company_id or t.tax_exigibility == 'on_payment'
                                   for t in line.tax_ids | line.tax_ids.flatten_taxes_hierarchy())):
                        raise ValidationError(_('Choose active taxes of the same company and invoice type.'))
                    commands.append(Command.update(line.original_line_id.id, {'name': line.name,
                        'quantity': float(quantity), 'price_unit': float(price), 'discount': float(discount),
                        'tax_ids': [Command.set(line.tax_ids.ids)]}))
                move_vals = {'partner_id': self.partner_id.id, 'invoice_line_ids': commands}
        return move_vals, payment_vals

    def action_confirm(self):
        self.ensure_one()
        self._require_owner()
        wizard = self.with_context(self._clean_context(self.company_id))
        with self.env.cr.savepoint():
            wizard._lock()
            wizard._require_owner()
            if wizard.completed:
                return {'type': 'ir.actions.act_window_close'}
            move, payment, batch_line = wizard.move_id, wizard.payment_id, wizard.batch_line_id
            wizard._validate_source(move, payment, batch_line, operation=wizard.operation)
            before = wizard._snapshot(move, payment, batch_line)
            if before != wizard.baseline_json:
                raise UserError(_('The source changed after this correction was opened. Close it and open a fresh correction.'))
            if wizard.operation == 'cancel':
                wizard._validate_reason()
                if payment and not wizard.payment_cancel_ack:
                    raise ValidationError(_('Confirm that this recorded payment was entered in error and must be cancelled. If money actually moved, use the native credit or refund workflow.'))
                move_vals, payment_vals = False, False
            else:
                move_vals, payment_vals = wizard._input_values()
            old_dates = {record.id: (record.date, record.invoice_date) for record in move | payment.move_id}
            from .lifecycle import lifecycle_scope
            wizard = lifecycle_scope(wizard, move, payment)
            move, payment, batch_line = wizard.move_id, wizard.payment_id, wizard.batch_line_id
            if wizard.operation == 'cancel':
                wizard._apply_cancellation(old_dates)
                if batch_line:
                    batch_line._baseer_apply_invoice_cancellation(wizard, CORRECTION_CAPABILITY)
            else:
                wizard._apply_edit(move_vals, payment_vals, old_dates)
                if batch_line:
                    batch_line._baseer_apply_invoice_correction(wizard, CORRECTION_CAPABILITY)
            after = wizard._snapshot(move, payment, batch_line)
            audit = wizard.env['baseer.financial.correction.audit']._record_correction(wizard, before, after, CORRECTION_CAPABILITY)
            super(FinancialCorrection, wizard).write({'completed': True, 'audit_id': audit.id})
            source = move or payment
            source.message_post(body=(_('Operation cancelled: %s', wizard.reason.strip())
                                      if wizard.operation == 'cancel' else _('Operation corrected: %s', wizard.reason.strip())))
        return {'type': 'ir.actions.act_window_close'}

    def _apply_edit(self, move_vals, payment_vals, old_dates):
        move, payment = self.move_id, self.payment_id
        if payment:
            payment._seek_for_lines()[1].remove_move_reconcile()
        if move:
            move.button_draft()
            move.write(move_vals)
            if _money(move.amount_total) <= 0:
                raise ValidationError(_('The corrected invoice total must remain positive.'))
            preserved_amount = _money(payment_vals['amount'] if payment_vals else payment.amount) if payment else Decimal('0')
            if preserved_amount > _money(move.amount_total):
                raise UserError(_('The payment exceeds the corrected invoice. Confirm the actual payment amount or use the native credit workflow.'))
            move.action_post()
        if payment_vals:
            payment.action_draft()
            payment.write(payment_vals)
            payment.action_post()
        if move and payment:
            terms = move.line_ids.filtered(lambda line: line.account_id.account_type in ('asset_receivable', 'liability_payable'))
            counterpart = payment._seek_for_lines()[1]
            if (len(terms.account_id) != 1 or terms.account_id != counterpart.account_id
                    or terms.partner_id != counterpart.partner_id):
                raise ValidationError(_('The corrected payment and invoice cannot be reconciled on the same counterparty account.'))
            (terms | counterpart).reconcile()
        self._verify_result(old_dates, payment_vals)

    def _apply_cancellation(self, old_dates):
        move, payment = self.move_id, self.payment_id
        original_lines = (move | payment.move_id).line_ids.ids
        if payment:
            payment._seek_for_lines()[1].remove_move_reconcile()
            if payment.move_id.state == 'posted':
                # Do not draft this payment first: native action_cancel deletes
                # draft payment moves, whereas posted history is retained.
                payment.action_cancel()
            elif payment.state != 'canceled':
                raise UserError(_('The payment status is inconsistent with its cancelled entry. Review its native source.'))
        if move and move.state == 'posted':
            move.button_cancel()
        self._verify_cancellation(old_dates, original_lines)

    def _verify_cancellation(self, old_dates, original_lines):
        move, payment = self.move_id, self.payment_id
        records = move | payment.move_id
        if set(records.exists().ids) != set(old_dates) or set(records.line_ids.ids) != set(original_lines):
            raise ValidationError(_('Cancellation must retain the original documents and journal items.'))
        for record in records:
            if record.state != 'cancel' or (record.date, record.invoice_date) != old_dates[record.id]:
                raise ValidationError(_('Cancellation did not close all affected entries on their original dates.'))
            if sum((_money(line.balance) for line in record.line_ids), Decimal('0')) != 0:
                raise ValidationError(_('The retained cancellation history is not balanced.'))
        if payment and payment.state != 'canceled':
            raise ValidationError(_('The recorded payment was not cancelled.'))
        if records.line_ids.matched_debit_ids or records.line_ids.matched_credit_ids:
            raise ValidationError(_('The cancelled operation still has an active reconciliation.'))

    def _verify_result(self, old_dates, payment_vals):
        move, payment = self.move_id, self.payment_id
        for record in move | payment.move_id:
            if record.state != 'posted' or (record.date, record.invoice_date) != old_dates[record.id]:
                raise ValidationError(_('The correction must keep the original posting dates and posted state.'))
            if sum((_money(line.balance) for line in record.line_ids), Decimal('0')) != 0:
                raise ValidationError(_('Native accounting did not produce a balanced entry.'))
        if self.batch_line_id and _money(move.amount_total) != _number(self.gross_amount_input, _('Invoice total'), minimum=_CENT):
            raise ValidationError(_('Native tax calculation does not match the requested invoice total.'))
        if payment:
            liquidity, counterpart, writeoffs = payment._seek_for_lines()
            wanted = _money(payment_vals['amount'] if payment_vals else payment.amount)
            signed = wanted if payment.payment_type == 'inbound' else -wanted
            if (len(liquidity) != 1 or len(counterpart) != 1 or writeoffs
                    or liquidity.account_id != payment.journal_id.default_account_id
                    or _money(liquidity.balance) != signed or payment.state not in ('paid', 'in_process')):
                raise ValidationError(_('The posted cash or bank movement does not match the actual payment.'))
            if move and _money(move.amount_residual) != _money(move.amount_total) - wanted:
                raise ValidationError(_('The corrected invoice residual does not match its payment.'))


class FinancialCorrectionLine(models.TransientModel):
    _name = 'baseer.financial.correction.line'
    _description = 'Financial Correction Invoice Line'
    wizard_id = fields.Many2one('baseer.financial.correction', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='wizard_id.company_id', store=True)
    original_line_id = fields.Many2one('account.move.line', required=True, readonly=True)
    name = fields.Char(required=True)
    quantity_input = fields.Char(required=True, string='Quantity')
    price_unit_input = fields.Char(required=True, string='Unit price')
    discount_input = fields.Char(required=True, string='Discount')
    tax_ids = fields.Many2many('account.tax', string='Taxes')

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_CAP_KEY) is not CORRECTION_CAPABILITY:
            raise AccessError(_('Correction lines are prepared from the source invoice.'))
        return super().create(vals_list)

    def write(self, vals):
        if {'wizard_id', 'company_id', 'original_line_id', 'create_uid'}.intersection(vals):
            raise AccessError(_('The original invoice line cannot be changed.'))
        self.wizard_id._require_owner()
        self.wizard_id._lock_wizard_rows()
        if any(self.wizard_id.mapped('completed')):
            raise AccessError(_('Completed corrections cannot be changed.'))
        return super().write(vals)


class FinancialCorrectionAudit(models.Model):
    _name = 'baseer.financial.correction.audit'
    _description = 'Financial Correction Audit'
    _order = 'id desc'
    company_id = fields.Many2one('res.company', required=True, index=True, readonly=True)
    move_id = fields.Many2one('account.move', index=True, readonly=True, ondelete='restrict')
    payment_id = fields.Many2one('account.payment', index=True, readonly=True, ondelete='restrict')
    batch_line_id = fields.Many2one('baseer.purchase.batch.line', index=True, readonly=True, ondelete='restrict')
    user_id = fields.Many2one('res.users', required=True, readonly=True)
    operation = fields.Selection([('edit', 'Edit'), ('cancel', 'Cancel operation')],
                                 default='edit', required=True, readonly=True)
    reason = fields.Text(required=True, readonly=True)
    before_json = fields.Text(required=True, readonly=True)
    after_json = fields.Text(required=True, readonly=True)
    operation_token = fields.Char(required=True, index=True, readonly=True)
    _operation_unique = models.Constraint('UNIQUE(operation_token)', 'This correction has already been recorded.')

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_('Correction audit records are created by the correction workflow only.'))

    @api.model
    def _record_correction(self, wizard, before, after, capability):
        if capability is not CORRECTION_CAPABILITY:
            raise AccessError(_('Invalid correction capability.'))
        wizard._require_owner()
        return super().create({'company_id': wizard.company_id.id, 'move_id': wizard.move_id.id,
            'payment_id': wizard.payment_id.id, 'batch_line_id': wizard.batch_line_id.id,
            'user_id': self.env.uid, 'reason': wizard.reason.strip(), 'before_json': before,
            'after_json': after, 'operation_token': wizard.operation_token,
            'operation': wizard.operation if 'operation' in wizard._fields else 'edit'})

    def write(self, vals):
        raise AccessError(_('Correction audit records cannot be changed.'))

    def unlink(self):
        raise AccessError(_('Correction audit records cannot be deleted.'))
