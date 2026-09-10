"""Choose the owning workflow before opening a financial input correction."""
from odoo import _, api, fields, models
from odoo.fields import Domain
from odoo.exceptions import AccessError, UserError


def can_correct(env):
    user = env.user
    if user.has_group('baseer_access_roles.group_cashier'):
        return False
    if user.has_group('baseer_access_roles.group_owner'):
        return True
    return (any(user.has_group(group) for group in (
        'baseer_access_roles.group_accountant', 'account.group_account_manager'))
        and user.sudo().baseer_allow_financial_correction)


def require_access(record):
    user = record.env.user
    if not can_correct(record.env):
        raise AccessError(_('Only an authorized accountant or owner can correct financial operations.'))
    record.check_access('write')
    for company in record.mapped('company_id'):
        if company != record.env.company or company not in user.company_ids:
            raise AccessError(_('Switch to the authorized company that owns this operation.'))


def linked(record, field):
    return record[field] if field in record._fields else False


def reject_hr_accounts(record, accounts):
    """Reject configured HR accounts without exposing protected company fields."""
    company = record.company_id.sudo()
    private_ids = set()
    for name in ('baseer_salary_expense_id', 'baseer_salary_payable_id',
                 'baseer_deduction_account_id', 'baseer_loan_account_id'):
        if name in company._fields:
            private_ids.update(company[name].ids)
    if private_ids.intersection(accounts.ids):
        raise UserError(_('Correct payroll and employee payments in their original workflow.'))


def open_source(source):
    source.check_access('read')
    action = source.get_formview_action() if len(source) == 1 else source._get_records_action()
    return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
        'type': 'info', 'message': source.env._('Review and correct this operation in its original workflow.'),
        'sticky': False, 'next': action,
    }}


class CorrectionSourcePrivacy(models.Model):
    _inherit = 'res.users'

    baseer_allow_financial_correction = fields.Boolean(
        string='Allow financial operation correction', default=True,
        groups='base.group_system,baseer_access_roles.group_owner')

    def _baseer_guard_correction_setting(self):
        user = self.env.user
        if not (user.has_group('base.group_system') or user.has_group('baseer_access_roles.group_owner')):
            raise AccessError(_('Only the owner or settings administrator can change correction permission.'))

    @api.model_create_multi
    def create(self, vals_list):
        if any('baseer_allow_financial_correction' in vals for vals in vals_list) or (
                'default_baseer_allow_financial_correction' in self.env.context):
            self._baseer_guard_correction_setting()
        return super().create(vals_list)

    def write(self, vals):
        if 'baseer_allow_financial_correction' in vals:
            self._baseer_guard_correction_setting()
        result = super().write(vals)
        if 'baseer_allow_financial_correction' in vals:
            for model in ('account.move', 'account.payment', 'baseer.purchase.batch.line'):
                self.env[model].invalidate_model(['baseer_can_correct_operation'])
        return result

    def _baseer_correction_source_domain(self, company_ids, prefix='', creator=False):
        """Embed current source rules; an any(True) relation is optimized away."""
        self.ensure_one()
        if prefix not in ('', 'wizard_id.'):
            raise ValueError('Unsupported correction source prefix')
        # ir.rule evaluates `user` as a sudo recordset. Compute source rules in
        # the real caller environment, including every installed privacy rule.
        rules = self.with_user(self.id).sudo(False).with_context(
            allowed_company_ids=list(company_ids)).env['ir.rule']
        domains = []
        for relation, model in (('move_id', 'account.move'), ('payment_id', 'account.payment')):
            source_domain = rules._compute_domain(model, 'read')
            domains.append(Domain.OR([
                Domain(relation, '=', False),
                Domain(relation, 'any!', source_domain),
            ]))
        result = Domain.AND(domains)
        if prefix:
            result = Domain('wizard_id', 'any!', result)
        result &= Domain('company_id', 'in', company_ids)
        if creator:
            result &= Domain(prefix + 'create_uid', '=', self.id)
        if self.has_group('baseer_access_roles.group_cashier'):
            return Domain.FALSE
        # Return the native Domain object intact: converting to a list loses
        # the trusted any! predicates, which Odoo rejects in domain lists.
        return result


class CorrectionMoveDispatcher(models.Model):
    _inherit = 'account.move'

    baseer_can_correct_operation = fields.Boolean(compute='_compute_baseer_can_correct_operation')

    @api.depends_context('uid')
    def _compute_baseer_can_correct_operation(self):
        permitted = can_correct(self.env)
        for record in self:
            record.baseer_can_correct_operation = permitted

    def _baseer_correction_require_access(self):
        require_access(self)
        return True

    def _baseer_correction_owner(self):
        """Fixed source fields only. Never accept a client model or source ID."""
        self.ensure_one()
        for field, action in (
            ('baseer_pos_summary_id', 'action_open_correction'),
            ('baseer_payslip_id', 'action_correct'),
            ('baseer_correction_payslip_id', 'action_correct'),
            ('baseer_loan_id', 'action_correct'),
            ('baseer_hr_service_id', None), ('baseer_eos_id', None),
            ('expense_ids', None), ('pos_order_ids', None),
            ('pos_session_ids', None), ('pos_payment_ids', None),
            ('statement_line_id', None), ('stock_move_id', None), ('asset_id', None),
        ):
            source = linked(self, field)
            if source:
                return source, action
        for field in ('sale_line_ids', 'purchase_line_id'):
            if field in self.invoice_line_ids._fields:
                lines = self.invoice_line_ids.mapped(field)
                if lines:
                    return lines.order_id, None
        return False, None

    def _baseer_assert_correction_eligible(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        source, _action = self._baseer_correction_owner()
        if source:
            raise UserError(_('This operation belongs to another workflow. Correct it from its original document.'))
        reject_hr_accounts(self, self.line_ids.account_id)
        if self.move_type not in ('out_invoice', 'out_refund', 'out_receipt', 'in_invoice', 'in_refund', 'in_receipt') and not self.origin_payment_id:
            raise UserError(_('Use the native accounting workflow to correct an independent journal entry.'))
        return True

    def _baseer_correction_batch_line(self):
        self.ensure_one()
        lines = self.env['baseer.purchase.batch.line'].search([('move_id', '=', self.id)], limit=2)
        if len(lines) > 1:
            raise UserError(_('This invoice has ambiguous purchase-batch ownership. Review its source links.'))
        return lines

    def action_baseer_correct_operation(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        source, action = self._baseer_correction_owner()
        if source:
            source.check_access('read')
            return getattr(source, action)() if action and len(source) == 1 else open_source(source)
        if self.origin_payment_id:
            return self.origin_payment_id.action_baseer_correct_operation()
        if self.move_type == 'entry':
            return self.action_reverse()
        self._baseer_assert_correction_eligible()
        batch_line = self._baseer_correction_batch_line()
        return self.env['baseer.financial.correction']._open_for_source(move=self, batch_line=batch_line)

    def action_baseer_cancel_operation(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        source, action = self._baseer_correction_owner()
        if source:
            if source._name == 'baseer.pos.summary':
                return source.action_baseer_cancel_operation()
            raise UserError(_('Cancel this operation from its original operational document.'))
        if self.origin_payment_id:
            return self.origin_payment_id.action_baseer_cancel_operation()
        self._baseer_assert_correction_eligible()
        return self.env['baseer.financial.correction']._open_for_source(
            move=self, batch_line=self._baseer_correction_batch_line(), operation='cancel')


class CorrectionPaymentDispatcher(models.Model):
    _inherit = 'account.payment'

    baseer_can_correct_operation = fields.Boolean(compute='_compute_baseer_can_correct_operation')

    @api.depends_context('uid')
    def _compute_baseer_can_correct_operation(self):
        permitted = can_correct(self.env)
        for record in self:
            record.baseer_can_correct_operation = permitted

    def _baseer_correction_require_access(self):
        require_access(self)
        return True

    def _baseer_assert_correction_eligible(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        self._baseer_assert_non_hr_payment()
        if self.move_id:
            self.move_id._baseer_assert_correction_eligible()
        if linked(self, 'expense_ids') or linked(self, 'baseer_pos_summary_id'):
            raise UserError(_('Correct this payment from its original operational document.'))
        return True

    def _baseer_assert_non_hr_payment(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        # Only fixed classification/existence checks use sudo; no HR data is returned.
        # The permanent marker remains authoritative after settlement vacuum or
        # changes to the company's salary account configuration.
        if linked(self.sudo(), 'baseer_private_hr'):
            raise UserError(_('Correct payroll and employee payments in their original workflow.'))
        for model in ('baseer.payroll.settlement', 'baseer.payroll.settlement.line'):
            if model in self.env and self.env[model].sudo().search_count(
                    [('payment_ids', 'in', self.ids)], limit=1):
                raise UserError(_('Correct payroll and employee payments in their original workflow.'))
        reject_hr_accounts(self, self.destination_account_id | self.move_id.line_ids.account_id)
        return True

    def action_baseer_correct_operation(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        self._baseer_assert_non_hr_payment()
        source = linked(self, 'expense_ids') or linked(self, 'baseer_pos_summary_id')
        if source:
            return open_source(source)
        if self.move_id:
            owner, action = self.move_id._baseer_correction_owner()
            if owner:
                owner.check_access('read')
                return getattr(owner, action)() if action and len(owner) == 1 else open_source(owner)
        self._baseer_assert_correction_eligible()
        lines = self.env['baseer.purchase.batch.line'].search([('payment_id', '=', self.id)], limit=2)
        if len(lines) > 1:
            raise UserError(_('This payment belongs to multiple purchase rows. Review the original settlement.'))
        return self.env['baseer.financial.correction']._open_for_source(payment=self,
            move=lines.move_id if lines else False, batch_line=lines)

    def action_baseer_cancel_operation(self):
        self.ensure_one()
        self._baseer_correction_require_access()
        self._baseer_assert_correction_eligible()
        return self.env['baseer.financial.correction']._open_for_source(payment=self, operation='cancel')


class CorrectionBatchDispatcher(models.Model):
    _inherit = 'baseer.purchase.batch.line'

    baseer_can_correct_operation = fields.Boolean(compute='_compute_baseer_can_correct_operation')

    @api.depends_context('uid')
    def _compute_baseer_can_correct_operation(self):
        permitted = can_correct(self.env)
        for record in self:
            record.baseer_can_correct_operation = permitted

    def action_baseer_correct_operation(self):
        self.ensure_one()
        require_access(self)
        if self.batch_id.state != 'approved' or not self.move_id:
            raise UserError(_('Only an approved purchase row with its original invoice can be corrected.'))
        self.move_id._baseer_assert_correction_eligible()
        return self.env['baseer.financial.correction']._open_for_source(
            move=self.move_id, payment=self.payment_id, batch_line=self)
