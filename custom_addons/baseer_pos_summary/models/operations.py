from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .common import active_company, clean_context, lifecycle_manager


def covered_slots(scope):
    return {'morning', 'evening'} if scope == 'all' else {scope}


def checked_range(date_from, date_to):
    first, last = fields.Date.to_date(date_from), fields.Date.to_date(date_to)
    if not first or not last or last < first or (last - first).days >= 366:
        raise ValidationError(_('Choose a valid date range of at most 366 days.'))
    return first, last


class PosClosure(models.Model):
    _name = 'baseer.pos.closure'
    _description = 'Sales operating closure'
    _order = 'date_from desc, id desc'
    _rec_name = 'name'

    name = fields.Char(readonly=True, copy=False, default=lambda self: _('New'))
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True, ondelete='restrict')
    date_from = fields.Date(required=True, default=fields.Date.context_today, index=True)
    date_to = fields.Date(required=True, default=fields.Date.context_today, index=True)
    period_scope = fields.Selection([('all', 'All Day'), ('morning', 'Morning'), ('evening', 'Evening')], required=True, default='all')
    reason = fields.Selection([('eid', 'Eid'), ('holiday', 'Holiday'), ('maintenance', 'Maintenance'), ('other', 'Other')], required=True, default='holiday')
    notes = fields.Text()
    state = fields.Selection([('draft', 'Draft'), ('confirmed', 'Confirmed'), ('cancelled', 'Cancelled')], required=True, readonly=True, default='draft', index=True, copy=False)
    is_archived = fields.Boolean(default=False, readonly=True, copy=False, string='Archived')
    confirmed_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    confirmed_at = fields.Datetime(readonly=True, copy=False)
    cancellation_reason = fields.Char(copy=False)
    cancelled_by_id = fields.Many2one('res.users', readonly=True, copy=False)
    cancelled_at = fields.Datetime(readonly=True, copy=False)
    _PROTECTED = {'name', 'state', 'confirmed_by_id', 'confirmed_at', 'cancelled_by_id', 'cancelled_at', 'is_archived'}

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for record in self:
            checked_range(record.date_from, record.date_to)

    def _serialize_company(self):
        self.ensure_one()
        active_company(self, self.company_id)
        self.env.cr.execute('UPDATE res_company SET id=id WHERE id=%s', [self.company_id.id])

    def _lock(self, operation='write'):
        self.check_access(operation)
        for record in self.sorted('id'):
            record._serialize_company()
        self.flush_recordset()
        if self:
            self.env.cr.execute('SELECT id FROM baseer_pos_closure WHERE id IN %s ORDER BY id FOR UPDATE', [tuple(self.ids)])
            self.invalidate_recordset()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if self._PROTECTED.intersection(vals):
                raise AccessError(_('Closure approval and audit fields are controlled by the server.'))
            active_company(self, self.env['res.company'].browse(vals.get('company_id') or self.env.company.id))
        scoped = clean_context(self, self.env.company)
        records = super(PosClosure, scoped).create([dict(vals, company_id=self.env.company.id) for vals in vals_list])
        for record in records:
            super(PosClosure, record).write({'name': 'POS-C/%06d' % record.id})
        return records

    def write(self, vals):
        if set(vals) == {'is_archived'}:
            lifecycle_manager(self)
            if not isinstance(vals['is_archived'], bool):
                raise ValidationError(_('Archive status must be enabled or disabled.'))
            self._lock()
            return super().write(vals)
        if self._PROTECTED.intersection(vals):
            raise AccessError(_('Closure approval and audit fields are controlled by the server.'))
        self._lock()
        for record in self:
            if record.state == 'cancelled' or (record.state == 'confirmed' and set(vals) != {'cancellation_reason'}):
                raise UserError(_('Confirmed or cancelled closures cannot be changed. Cancel the closure with a reason instead.'))
            if record.state == 'confirmed' and not self.env.user.has_group('point_of_sale.group_pos_manager'):
                raise AccessError(_('Only a POS manager can cancel a closure.'))
            if 'company_id' in vals and vals['company_id'] != record.company_id.id:
                raise ValidationError(_('The closure company cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        self._lock('unlink')
        if any(record.state != 'draft' for record in self):
            raise UserError(_('Only draft closures can be deleted.'))
        return super().unlink()

    def action_archive(self):
        return self.write({'is_archived': True})

    def action_unarchive(self):
        return self.write({'is_archived': False})

    @api.model
    def _check_summary(self, summary):
        closures = self.search([('company_id', '=', summary.company_id.id), ('state', '=', 'confirmed'), ('date_from', '<=', summary.business_date), ('date_to', '>=', summary.business_date)])
        if any(covered_slots(row.period_scope) & covered_slots(summary.period_scope) for row in closures):
            raise ValidationError(_('This sales period is closed. Cancel the closure before entering sales.'))

    def action_confirm(self):
        with self.env.cr.savepoint():
            self._lock()
            for record in self:
                if record.state == 'confirmed':
                    continue
                if record.state != 'draft':
                    raise UserError(_('Only draft closures can be confirmed.'))
                record._check_dates()
                others = self.search([('company_id', '=', record.company_id.id), ('state', '=', 'confirmed'), ('date_from', '<=', record.date_to), ('date_to', '>=', record.date_from), ('id', '!=', record.id)])
                if any(covered_slots(row.period_scope) & covered_slots(record.period_scope) for row in others):
                    raise ValidationError(_('The closure overlaps another confirmed closure.'))
                summaries = self.env['baseer.pos.summary'].search([('company_id', '=', record.company_id.id), ('business_date', '>=', record.date_from), ('business_date', '<=', record.date_to), ('state', '!=', 'cancelled')])
                if any(covered_slots(row.period_scope) & covered_slots(record.period_scope) for row in summaries):
                    raise ValidationError(_('The closure overlaps an existing sales summary, including drafts.'))
                super(PosClosure, record).write({'state': 'confirmed', 'confirmed_by_id': self.env.uid, 'confirmed_at': fields.Datetime.now()})
        return True

    def action_cancel(self):
        if not self.env.user.has_group('point_of_sale.group_pos_manager'):
            raise AccessError(_('Only a POS manager can cancel a closure.'))
        with self.env.cr.savepoint():
            self._lock()
            for record in self:
                if record.state != 'confirmed':
                    raise UserError(_('Only confirmed closures can be cancelled.'))
                if not record.cancellation_reason or not record.cancellation_reason.strip():
                    raise ValidationError(_('Enter a cancellation reason first.'))
                super(PosClosure, record).write({'state': 'cancelled', 'cancelled_by_id': self.env.uid, 'cancelled_at': fields.Datetime.now()})
        return True
