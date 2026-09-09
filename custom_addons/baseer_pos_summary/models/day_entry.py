"""One entry screen, with the existing per-shift summaries as the authority."""
from decimal import Decimal
from urllib.parse import urlencode

from odoo import _, api, fields, models, Command
from odoo.exceptions import AccessError, UserError, ValidationError

from .common import ZERO, money, positive_amount, active_company, clean_context, internal, TOKEN_KEY, INTERNAL_TOKEN, lifecycle_manager


class PosDayEntry(models.TransientModel):
    _name = 'baseer.pos.day.entry'
    _description = 'Daily sales entry'

    company_id = fields.Many2one('res.company', required=True, readonly=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    config_id = fields.Many2one('pos.config', readonly=True, default=lambda self: self._default_config())
    business_date = fields.Date(required=True, default=fields.Date.context_today)
    day_off = fields.Boolean(string='DAY OFF')
    date_to = fields.Date(default=fields.Date.context_today, string='To date')
    closure_reason = fields.Selection(
        selection=lambda self: self.env['baseer.pos.closure']._fields['reason']._description_selection(self.env), default='holiday', string='Reason')
    closure_notes = fields.Text(string='Reason details')
    saved_closure_id = fields.Many2one('baseer.pos.closure', readonly=True, copy=False, ondelete='set null')
    day_schedule = fields.Selection([('all', 'All day'), ('split', 'Morning and evening'), ('morning', 'Morning only'), ('evening', 'Evening only')], default=False)
    pick_morning = fields.Boolean(string='Morning shift', compute='_compute_shift_picks', readonly=False)
    pick_evening = fields.Boolean(string='Evening shift', compute='_compute_shift_picks', readonly=False)
    pick_all = fields.Boolean(string='Full day', compute='_compute_shift_picks', readonly=False)
    first_customers = fields.Integer(default=0)
    second_customers = fields.Integer(default=0)
    first_notes = fields.Text()
    second_notes = fields.Text()
    first_zero_sales = fields.Boolean()
    second_zero_sales = fields.Boolean()
    first_allocation_ids = fields.One2many('baseer.pos.day.entry.line', 'entry_id', domain=[('slot', '=', 'first')])
    second_allocation_ids = fields.One2many('baseer.pos.day.entry.line', 'entry_id', domain=[('slot', '=', 'second')])
    saved_summary_ids = fields.Many2many('baseer.pos.summary', readonly=True, copy=False)
    state = fields.Selection([('draft', 'Entry'), ('saved', 'Saved drafts'), ('approved', 'Approved')], default='draft', readonly=True, required=True, copy=False)
    saved = fields.Boolean(compute='_compute_saved')
    is_archived = fields.Boolean(compute='_compute_archived', string='Archived')
    first_missing = fields.Boolean(compute='_compute_source_status')
    second_missing = fields.Boolean(compute='_compute_source_status')
    first_state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('cancelled', 'Cancelled for correction')], compute='_compute_source_status')
    second_state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('cancelled', 'Cancelled for correction')], compute='_compute_source_status')
    first_total = fields.Monetary(compute='_compute_totals')
    second_total = fields.Monetary(compute='_compute_totals')
    amount_total = fields.Monetary(compute='_compute_totals')
    customer_total = fields.Integer(compute='_compute_totals')
    average_per_customer = fields.Monetary(compute='_compute_totals')
    _INPUTS = {'company_id', 'config_id', 'business_date', 'day_schedule', 'first_customers', 'second_customers',
               'first_notes', 'second_notes', 'first_zero_sales', 'second_zero_sales', 'first_allocation_ids', 'second_allocation_ids',
               'day_off', 'date_to', 'closure_reason', 'closure_notes'}

    @api.model
    def _default_config(self):
        return self.env['baseer.pos.summary']._default_config()

    @api.model
    def default_get(self, fields_list):
        scoped = clean_context(self, self.env.company)
        defaults = super(PosDayEntry, scoped).default_get(fields_list)
        config = scoped._default_config()
        methods = config.payment_method_ids.filtered(lambda row: row.active and row.baseer_category_id.active)
        if len(methods) > 25:
            raise ValidationError(_('An external summary supports at most 25 payment methods.'))
        for slot in ('first', 'second'):
            field = slot + '_allocation_ids'
            if field in fields_list and config:
                defaults[field] = [Command.create({'slot': slot, 'payment_method_id': method.id, 'amount': 0})
                                   for method in methods]
        return defaults

    @api.depends('business_date')
    def _compute_display_name(self):
        for entry in self:
            entry.display_name = _('Sales summary')

    @api.depends('day_schedule')
    def _compute_shift_picks(self):
        for entry in self:
            entry.pick_morning = entry.day_schedule in ('morning', 'split')
            entry.pick_evening = entry.day_schedule in ('evening', 'split')
            entry.pick_all = entry.day_schedule == 'all'

    @api.onchange('pick_morning', 'pick_evening', 'pick_all')
    def _onchange_shift_picks(self):
        # Snapshot before changing the canonical selection: assigning it marks
        # the computed UI flags dirty and reading them again loses the click.
        old = self.day_schedule
        morning, evening, all_day = self.pick_morning, self.pick_evening, self.pick_all
        if self.state != 'draft':
            self._compute_shift_picks()
            return
        if old == 'all' and (morning or evening):
            new = 'split' if morning and evening else ('morning' if morning else 'evening')
        elif all_day:
            new = 'all'
        elif morning or evening:
            new = 'split' if morning and evening else ('morning' if morning else 'evening')
        else:
            self._compute_shift_picks()
            return
        if new == old:
            self._compute_shift_picks()
            return

        first = self.first_allocation_ids.filtered(lambda line: line.slot == 'first')
        second = self.second_allocation_ids.filtered(lambda line: line.slot == 'second')
        card_fields = ('customers', 'notes', 'zero_sales')
        has_data = (any(line.amount for line in first | second)
                    or any(self[slot + '_' + field] for slot in ('first', 'second') for field in card_fields))
        if 'all' in (old, new) and has_data:
            self._compute_shift_picks()
            return {'warning': {'title': _('Shift selection'), 'message': _('Clear the entered amounts, customers, notes and no-sales declarations before switching to or from Full day.')}}

        if (old == 'evening' and new in ('morning', 'split')) or (new == 'evening' and old in ('morning', 'split')):
            first_methods, second_methods = set(first.payment_method_id.ids), set(second.payment_method_id.ids)
            if first_methods != second_methods or len(first_methods) != len(first) or len(second_methods) != len(second):
                self._compute_shift_picks()
                return {'warning': {'title': _('Shift selection'), 'message': _('Both shift cards must contain the same payment methods before switching Evening only. Keep the current selection or start a new entry.')}}
            # Move values, never one2many slots/records. This preserves all
            # configured amounts and keeps the existing line guards intact.
            first_values = {line.payment_method_id.id: line.amount for line in first}
            second_values = {line.payment_method_id.id: line.amount for line in second}
            first_meta = {field: self['first_' + field] for field in card_fields}
            second_meta = {field: self['second_' + field] for field in card_fields}
            for line in first:
                line.amount = second_values[line.payment_method_id.id]
            for line in second:
                line.amount = first_values[line.payment_method_id.id]
            for field in card_fields:
                self['first_' + field] = second_meta[field]
                self['second_' + field] = first_meta[field]
        self.day_schedule = new
        self._compute_shift_picks()

    @api.onchange('business_date', 'day_off')
    def _onchange_closure_date(self):
        if self.day_off and self.business_date and (not self.date_to or self.date_to < self.business_date):
            self.date_to = self.business_date

    @api.onchange('first_zero_sales', 'second_zero_sales')
    def _onchange_zero_sales(self):
        for slot in ('first', 'second'):
            if self[slot + '_zero_sales']:
                self[slot + '_customers'] = 0
                for line in self[slot + '_allocation_ids']:
                    if line.slot == slot:
                        line.amount = 0

    @api.depends('state')
    def _compute_saved(self):
        for entry in self:
            entry.saved = entry.state != 'draft'

    @api.depends('saved_summary_ids.is_archived', 'saved_closure_id.is_archived', 'day_off')
    def _compute_archived(self):
        for entry in self:
            entry.is_archived = (entry.saved_closure_id.is_archived if entry.day_off else
                                 bool(entry.saved_summary_ids) and all(entry.saved_summary_ids.mapped('is_archived')))

    def _lifecycle_day(self):
        self.ensure_one()
        lifecycle_manager(self)
        self._authorize('read')
        domain = [('company_id', '=', self.company_id.id)]
        if self.day_off:
            domain.append(('closure_id', '=', self.saved_closure_id.id))
        else:
            domain.extend([('business_date', '=', self.business_date), ('is_day_off', '=', False)])
        day = self.env['baseer.pos.day.archive'].search(domain)
        if not self.saved or not day:
            raise UserError(_('The saved day is no longer available. Refresh the summaries list.'))
        summaries, closures = day._get_sources()
        if ((self.day_off and (not self.saved_closure_id or closures != self.saved_closure_id)) or
                (not self.day_off and (not self.saved_summary_ids or set(summaries.ids) != set(self.saved_summary_ids.ids)))):
            raise UserError(_('The saved day is no longer available. Refresh the summaries list.'))
        return day

    def action_archive(self):
        return self._lifecycle_day().action_archive()

    def action_unarchive(self):
        return self._lifecycle_day().action_unarchive()

    def action_delete_drafts(self):
        self._lifecycle_day().action_delete_drafts()
        action = self.env['ir.actions.actions']._for_xml_id('baseer_pos_summary.action_summary')
        action['domain'] = [('company_id', '=', self.company_id.id)]
        return action

    @api.depends('saved_summary_ids.period_scope', 'saved_summary_ids.state', 'day_schedule', 'state', 'day_off')
    def _compute_source_status(self):
        for entry in self:
            first_period = 'morning' if entry.day_schedule == 'split' else entry.day_schedule
            first = entry.saved_summary_ids.filtered(lambda row: row.period_scope == first_period)
            second = entry.saved_summary_ids.filtered(lambda row: row.period_scope == 'evening') if entry.day_schedule == 'split' else self.env['baseer.pos.summary']
            entry.first_missing = entry.saved and not entry.day_off and not first
            entry.second_missing = entry.saved and not entry.day_off and entry.day_schedule == 'split' and not second
            entry.first_state = first[:1].state or False
            entry.second_state = second[:1].state or False

    @api.model
    def _from_summaries(self, summaries):
        """Reopen durable source records; the temporary form owns no history."""
        summaries = summaries.exists().filtered(lambda row: row.state != 'cancelled')
        summaries.check_access('read')
        if not summaries or len(summaries.company_id) != 1 or len(set(summaries.mapped('business_date'))) != 1:
            raise UserError(_('Select the summaries of one company and one sales day.'))
        active_company(self, summaries.company_id)
        if len(summaries.config_id) != 1 or len(set(summaries.mapped('day_schedule'))) != 1:
            raise UserError(_('The saved shifts use different configurations. Open their source details to review them.'))
        schedule = summaries[0].day_schedule
        values = {'company_id': summaries.company_id.id, 'config_id': summaries.config_id.id,
                  'business_date': summaries[0].business_date, 'day_schedule': schedule,
                  'saved_summary_ids': [Command.set(summaries.ids)],
                  'state': 'approved' if all(row.state == 'approved' for row in summaries) else 'saved',
                  'first_allocation_ids': [], 'second_allocation_ids': []}
        for summary in summaries:
            slot = 'second' if schedule == 'split' and summary.period_scope == 'evening' else 'first'
            values.update({slot + '_customers': summary.customer_count, slot + '_notes': summary.notes,
                           slot + '_zero_sales': summary.zero_sales,
                           slot + '_allocation_ids': [Command.create({'slot': slot, 'payment_method_id': line.payment_method_id.id,
                                                                      'amount': line.amount}) for line in summary.allocation_ids]})
        # Historical methods can be inactive today. Only this private, checked
        # source-to-readonly snapshot path bypasses new-entry configuration checks.
        scoped = clean_context(self, summaries.company_id, authorized_internal=True)
        return super(PosDayEntry, scoped).create(values)

    @api.model
    def _from_closure(self, closure):
        closure.ensure_one()
        closure.check_access('read')
        active_company(self, closure.company_id)
        if closure.state != 'confirmed' or closure.period_scope != 'all':
            raise UserError(_('This full-day closure is no longer confirmed. Refresh the summaries list.'))
        scoped = clean_context(self, closure.company_id, authorized_internal=True)
        return super(PosDayEntry, scoped).create({
            'company_id': closure.company_id.id, 'day_off': True, 'business_date': closure.date_from,
            'date_to': closure.date_to, 'closure_reason': closure.reason, 'closure_notes': closure.notes,
            'saved_closure_id': closure.id, 'state': 'approved', 'first_allocation_ids': [], 'second_allocation_ids': []})

    @api.depends('first_allocation_ids.amount', 'second_allocation_ids.amount', 'first_customers', 'second_customers', 'day_schedule')
    def _compute_totals(self):
        for entry in self:
            first = sum((money(row.amount) for row in entry.first_allocation_ids if row.slot == 'first'), ZERO)
            second = sum((money(row.amount) for row in entry.second_allocation_ids if row.slot == 'second'), ZERO)
            gross = first + (second if entry.day_schedule == 'split' else ZERO)
            customers = entry.first_customers + (entry.second_customers if entry.day_schedule == 'split' else 0)
            entry.first_total, entry.second_total, entry.amount_total = float(first), float(second), float(gross)
            entry.customer_total = customers
            entry.average_per_customer = float(money(gross / Decimal(customers))) if customers else 0

    def _authorize(self, operation='write'):
        self.check_access(operation)
        for entry in self:
            active_company(entry, entry.company_id)
            if entry.create_uid != self.env.user:
                raise AccessError(_('Only the entry creator can use this sales entry.'))

    def _lock(self, operation='write', serialize_company=True):
        self._authorize(operation)
        self.flush_recordset()
        if self:
            self.env.cr.execute('SELECT id FROM baseer_pos_day_entry WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(sorted(self.ids))])
            self.invalidate_recordset()
            if serialize_company:
                for entry in self:
                    self.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [entry.company_id.id])

    def _require_draft(self):
        if any(entry.state != 'draft' for entry in self):
            raise UserError(_('This entry has already created its summaries and cannot be changed.'))

    @api.model
    def _normalize(self, vals, creating=False):
        # These nonstored flags are a UI projection, never another source of
        # schedule semantics. The onchange persists the canonical selection.
        vals = {key: value for key, value in vals.items() if key not in {'pick_morning', 'pick_evening', 'pick_all'}}
        if set(vals) - self._INPUTS:
            raise AccessError(_('Entry status, totals and summary links are controlled by the server.'))
        normalized = dict(vals)
        if 'day_off' in vals and not isinstance(vals['day_off'], bool):
            raise ValidationError(_('DAY OFF must be a checkbox value.'))
        for slot in ('first', 'second'):
            zero = slot + '_zero_sales'
            if zero in vals and not isinstance(vals[zero], bool):
                raise ValidationError(_('The no-sales declaration must be a checkbox value.'))
            count = slot + '_customers'
            if count in vals:
                self.env['baseer.pos.summary']._validate_values({'customer_count': vals[count]})
            field = slot + '_allocation_ids'
            if field in vals:
                commands = []
                for command in vals[field]:
                    operation = command[0]
                    if operation not in (0, 1, 2) or (creating and operation != 0):
                        raise ValidationError(_('Payment slots must be created or edited within their own entry.'))
                    if operation == 0:
                        payload = dict(command[2])
                        if 'slot' in payload and payload['slot'] != slot:
                            raise ValidationError(_('The payment slot does not match its shift card.'))
                        payload['slot'] = slot
                        commands.append(Command.create(payload))
                    else:
                        self.ensure_one()
                        line = self.env['baseer.pos.day.entry.line'].browse(command[1])
                        line.check_access('read')
                        if line.entry_id != self or line.slot != slot:
                            raise AccessError(_('Payment lines must remain in their own shift card.'))
                        commands.append(command)
                normalized[field] = commands
        return normalized

    @api.model_create_multi
    def create(self, vals_list):
        scoped = clean_context(self, self.env.company)
        normalized = []
        for vals in vals_list:
            vals = self._normalize(vals, creating=True)
            active_company(self, self.env['res.company'].browse(vals.get('company_id') or self.env.company.id))
            config = self.env['pos.config'].browse(vals.get('config_id')) if vals.get('config_id') else self._default_config()
            if not vals.get('day_off') and (not config or config.company_id != self.env.company or not config.baseer_summary_only):
                raise ValidationError(_('Select the dedicated external-summary POS for this company.'))
            if config and config.company_id != self.env.company:
                raise AccessError(_('The entry company and POS configuration cannot be changed.'))
            normalized.append(dict(vals, company_id=self.env.company.id, config_id=config.id))
        with self.env.cr.savepoint():
            return super(PosDayEntry, scoped).create(normalized)

    def write(self, vals):
        with self.env.cr.savepoint():
            self._lock()
            self._require_draft()
            for entry in self:
                for field in ('company_id', 'config_id'):
                    if field in vals and vals[field] != entry[field].id:
                        raise AccessError(_('The entry company and POS configuration cannot be changed.'))
                super(PosDayEntry, entry).write(entry._normalize(vals))
        return True

    def unlink(self):
        if internal(self):
            return super().unlink()
        self._lock('unlink')
        self._require_draft()
        return super().unlink()

    def _transient_clean_rows_older_than(self, seconds):
        return super(PosDayEntry, self.with_context(**{TOKEN_KEY: INTERNAL_TOKEN}))._transient_clean_rows_older_than(seconds)

    def _summary_values(self, slot, period):
        self.ensure_one()
        lines = self[slot + '_allocation_ids'].filtered(lambda row: row.slot == slot)
        return {'company_id': self.company_id.id, 'config_id': self.config_id.id,
                'business_date': self.business_date, 'day_schedule': self.day_schedule, 'period_scope': period,
                'customer_count': self[slot + '_customers'], 'zero_sales': self[slot + '_zero_sales'],
                'notes': self[slot + '_notes'], 'allocation_ids': [Command.create({'payment_method_id': row.payment_method_id.id, 'amount': row.amount}) for row in lines]}

    def _form_action(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'current'}

    def action_save(self):
        self.ensure_one()
        if self.day_off:
            return self._save_day_off()
        with self.env.cr.savepoint():
            self._lock()
            if self.state != 'draft':
                return self._form_action()
            if not self.day_schedule:
                raise ValidationError(_('Select a shift before saving sales.'))
            self._validate_lines()
            period = 'morning' if self.day_schedule == 'split' else self.day_schedule
            values = [self._summary_values('first', period)]
            if self.day_schedule == 'split':
                values.append(self._summary_values('second', 'evening'))
            summaries = self.env['baseer.pos.summary'].create(values)
            super(PosDayEntry, self).write({'saved_summary_ids': [Command.set(summaries.ids)], 'state': 'saved'})
        return self._form_action()

    def action_save_and_approve(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            self._lock(serialize_company=False)
            if self.day_off:
                return self._save_day_off()
            if self.state == 'draft':
                self.action_save()
            self.action_approve()
        return self._form_action()

    def _save_day_off(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            self._lock(serialize_company=False)
            if self.state != 'draft':
                if not self.saved_closure_id or self.saved_closure_id.state != 'confirmed':
                    raise UserError(_('This full-day closure is no longer confirmed. Refresh the summaries list.'))
                return self._form_action()
            if not self.day_off:
                raise UserError(_('Select DAY OFF before saving a closure.'))
            if self.closure_reason == 'other' and not (self.closure_notes or '').strip():
                raise ValidationError(_('Write the reason when selecting Other.'))
            closure = self.env['baseer.pos.closure'].create({
                'company_id': self.company_id.id, 'date_from': self.business_date, 'date_to': self.date_to,
                'period_scope': 'all', 'reason': self.closure_reason, 'notes': self.closure_notes})
            closure.action_confirm()
            super(PosDayEntry, self).write({'saved_closure_id': closure.id, 'state': 'approved'})
        return self._form_action()

    def _validate_lines(self):
        for entry in self:
            for slot in ('first', 'second'):
                lines = entry[slot + '_allocation_ids'].filtered(lambda row: row.slot == slot)
                if len(lines) > 25 or len(lines.payment_method_id) != len(lines):
                    raise ValidationError(_('Use at most 25 distinct payment methods in each shift.'))
                if any(line.payment_method_id not in entry.config_id.payment_method_ids or not line.payment_method_id.active or not line.payment_method_id.baseer_category_id.active for line in lines):
                    raise ValidationError(_('Use only the payment methods configured for this company.'))

    def action_approve(self):
        self.ensure_one()
        if self.day_off:
            return self._save_day_off()
        with self.env.cr.savepoint():
            self._lock(serialize_company=False)
            if self.state == 'approved':
                return self._form_action()
            expected_periods = {'morning', 'evening'} if self.day_schedule == 'split' else {self.day_schedule}
            if self.state != 'saved' or len(self.saved_summary_ids) != len(expected_periods) or set(self.saved_summary_ids.mapped('period_scope')) != expected_periods:
                raise UserError(_('Save the entry before approving its summaries.'))
            self.saved_summary_ids._lock()
            self.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [self.company_id.id])
            # A draft may also be edited from the ordinary summary list. Do not
            # approve changed amounts while the entry still displays its snapshot.
            for summary in self.saved_summary_ids:
                slot = 'second' if self.day_schedule == 'split' and summary.period_scope == 'evening' else 'first'
                original = self._summary_values(slot, summary.period_scope)
                source_amounts = {line.payment_method_id.id: money(line.amount) for line in summary.allocation_ids}
                entry_amounts = {line.payment_method_id.id: money(line.amount) for line in self[slot + '_allocation_ids'] if line.slot == slot}
                if any(summary[key] != original[key] for key in ('business_date', 'day_schedule', 'customer_count', 'zero_sales', 'notes')) or source_amounts != entry_amounts:
                    raise UserError(_('A linked summary changed. Review and approve it from the sales summaries list.'))
                summary.action_approve()
            super(PosDayEntry, self).write({'state': 'approved'})
        return self._form_action()

    def action_view_summaries(self):
        self.ensure_one()
        self._authorize('read')
        return {'type': 'ir.actions.act_window', 'name': _('Sales summaries'), 'res_model': 'baseer.pos.summary', 'view_mode': 'list,form',
                'domain': [('id', 'in', self.saved_summary_ids.ids), ('company_id', '=', self.company_id.id)], 'context': {'create': False}}

    def action_view_closure(self):
        self.ensure_one()
        self._authorize('read')
        self.saved_closure_id.check_access('read')
        return {'type': 'ir.actions.act_window', 'res_model': 'baseer.pos.closure',
                'res_id': self.saved_closure_id.id, 'view_mode': 'form', 'target': 'current'}

    def action_share_whatsapp(self):
        self.ensure_one()
        self._authorize('read')
        if self.day_off:
            closure = self.saved_closure_id
            closure.check_access('read')
            if not closure or closure.state != 'confirmed':
                raise UserError(_('Save and confirm the closure before preparing its WhatsApp message.'))
            reason = dict(closure._fields['reason']._description_selection(self.env))[closure.reason]
            message = '\n'.join([_('DAY OFF'), closure.company_id.name,
                '%s — %s' % (closure.date_from, closure.date_to), _('Reason: %s', reason), closure.notes or '',
                _('Closed days are excluded from daily sales and customer averages.')])
            return {'type': 'ir.actions.act_url', 'url': 'https://web.whatsapp.com/send?' + urlencode({'text': message}), 'target': 'new'}
        summaries = self.env['baseer.pos.summary'].search([
            ('company_id', '=', self.company_id.id), ('business_date', '=', self.business_date), ('state', '!=', 'cancelled')])
        if not self.saved_summary_ids or not summaries or any(row.state != 'approved' for row in summaries):
            raise UserError(_('Save and approve all recorded shifts before preparing the daily WhatsApp report.'))
        summaries.check_access('read')
        periods = {'morning': 0, 'evening': 1, 'all': 2}
        summaries = summaries.sorted(lambda row: periods[row.period_scope])
        gross = sum((money(row.amount_gross) for row in summaries), ZERO)
        tax = sum((money(row.amount_tax) for row in summaries), ZERO)
        customers = sum(summaries.mapped('customer_count'))
        amount = lambda value: self.currency_id.format(float(money(value)))
        lines = [_('Daily sales summary'), self.company_id.name, str(self.business_date)]
        coverage = self.env['baseer.pos.daily.report']._aggregate_days(self.company_id, self.business_date, self.business_date)
        if coverage['days'][0]['status'] == 'incomplete':
            lines.append(_('Some scheduled shifts have not been recorded.'))
        collections = {}
        for summary in summaries:
            data = summary._get_print_data()
            lines.extend(['', '*%s*' % data['period'], _('Customers: %s', data['customers']),
                          _('Gross sales: %s', data['gross']), _('Average per customer: %s', data['average'])])
            for allocation in summary.allocation_ids:
                if money(allocation.amount) > ZERO:
                    lines.append('%s: %s' % (allocation.payment_method_id.display_name, amount(allocation.amount)))
                    method = allocation.payment_method_id
                    collections[method] = collections.get(method, ZERO) + money(allocation.amount)
            if summary.notes:
                lines.append(_('Notes: %s', summary.notes))
        lines.extend(['', '*' + _('Daily total') + '*', _('Gross sales: %s', amount(gross)),
                      _('Tax: %s', amount(tax)), _('Customers: %s', str(customers)),
                      _('Average per customer: %s', amount(gross / Decimal(customers)) if customers else amount(ZERO)),
                      '', _('Daily collections')])
        lines.extend('%s: %s' % (method.display_name, amount(value)) for method, value in collections.items())
        return {'type': 'ir.actions.act_url', 'url': 'https://web.whatsapp.com/send?' + urlencode({'text': '\n'.join(lines)}), 'target': 'new'}

    def action_save_and_share(self):
        self.ensure_one()
        with self.env.cr.savepoint():
            self.action_save_and_approve()
            return self.action_share_whatsapp()


class PosDayEntryLine(models.TransientModel):
    _name = 'baseer.pos.day.entry.line'
    _description = 'Daily sales payment slot'
    _order = 'id'

    entry_id = fields.Many2one('baseer.pos.day.entry', required=True, ondelete='cascade', index=True)
    slot = fields.Selection([('first', 'First shift'), ('second', 'Second shift')], required=True, index=True)
    company_id = fields.Many2one(related='entry_id.company_id', store=True)
    currency_id = fields.Many2one(related='entry_id.currency_id')
    payment_method_id = fields.Many2one('pos.payment.method', required=True, ondelete='restrict')
    amount = fields.Monetary(default=0)
    _INPUTS = {'entry_id', 'slot', 'payment_method_id', 'amount'}

    @api.model
    def _validate_values(self, vals):
        if set(vals) - self._INPUTS:
            raise AccessError(_('Payment slot metadata is controlled by the server.'))
        if 'amount' in vals:
            positive_amount(vals['amount'], self.env, allow_zero=True)

    @api.model_create_multi
    def create(self, vals_list):
        if internal(self):
            return super().create(vals_list)
        entries = self.env['baseer.pos.day.entry']
        for vals in vals_list:
            self._validate_values(vals)
            entry = self.env['baseer.pos.day.entry'].browse(vals.get('entry_id'))
            if not entry:
                raise ValidationError(_('Create payment slots inside a sales entry.'))
            entries |= entry
        entries._lock()
        entries._require_draft()
        with self.env.cr.savepoint():
            records = super(PosDayEntryLine, clean_context(self, self.env.company)).create(vals_list)
            entries.invalidate_recordset(['first_allocation_ids', 'second_allocation_ids'])
            entries._validate_lines()
            return records

    def write(self, vals):
        self.check_access('write')
        self._validate_values(vals)
        if {'entry_id', 'slot'}.intersection(vals):
            raise AccessError(_('Payment lines must remain in their own shift card.'))
        entries = self.entry_id
        entries._lock()
        entries._require_draft()
        with self.env.cr.savepoint():
            result = super().write(vals)
            entries._validate_lines()
            return result

    def unlink(self):
        if internal(self):
            return super().unlink()
        self.check_access('unlink')
        self.entry_id._lock()
        self.entry_id._require_draft()
        return super().unlink()

    def _transient_clean_rows_older_than(self, seconds):
        return super(PosDayEntryLine, self.with_context(**{TOKEN_KEY: INTERNAL_TOKEN}))._transient_clean_rows_older_than(seconds)
