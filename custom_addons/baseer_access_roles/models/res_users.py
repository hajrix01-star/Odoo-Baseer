from odoo import api, fields, models, Command
from odoo.exceptions import AccessError, ValidationError


ROLE_NAMES = ('owner', 'accountant', 'cashier')


class ResUsers(models.Model):
    _inherit = 'res.users'

    baseer_access_role = fields.Selection(
        [('owner', 'Owner / General Manager'), ('accountant', 'Accountant'),
         ('cashier', 'Cashier')], string='Baseer Access Role', copy=False,
        help='Leave empty to manage access manually. Applying a preset replaces existing application rights.',
    )

    @api.model
    def _baseer_require_role_admin(self):
        if not self.env.su and not self.env.user.has_group('base.group_system'):
            raise AccessError(self.env._('Only an administrator can change access roles or memberships.'))

    @api.model
    def _baseer_role_values(self, values):
        values = dict(values)
        role = values.get('baseer_access_role')
        if role:
            if role not in ROLE_NAMES:
                raise ValidationError(self.env._('Unknown access role.'))
            group = self.env.ref('baseer_access_roles.group_' + role)
            # Reset the explicit grants atomically, including grants left by an old administrator preset.
            values['group_ids'] = [Command.set(group.ids)]
            values.pop('role', None)
            if role == 'owner':
                values['company_ids'] = [Command.set(self.env['res.company'].sudo().search([]).ids)]
        elif 'baseer_access_role' in values and 'group_ids' not in values:
            # Switching to manual access starts with internal-user rights, never an invisible retained preset.
            values['group_ids'] = [Command.set(self.env.ref('base.group_user').ids)]
        if 'baseer_access_role' in values and values.get('notification_type') == 'inbox':
            values['group_ids'] = [*values['group_ids'], Command.link(self.env.ref('mail.group_mail_notification_type_inbox').id)]
        return values

    @api.onchange('baseer_access_role')
    def _onchange_baseer_access_role(self):
        self._baseer_require_role_admin()
        for user in self:
            values = user._baseer_role_values({
                'baseer_access_role': user.baseer_access_role,
                'notification_type': user.notification_type,
            })
            user.update(values)

    @api.model_create_multi
    def create(self, vals_list):
        guarded = {'baseer_access_role', 'group_ids', 'company_ids', 'role'}
        if any(guarded.intersection(vals) for vals in vals_list):
            self._baseer_require_role_admin()
        with self.env.cr.savepoint():
            users = super().create([self._baseer_role_values(vals) for vals in vals_list])
            users._baseer_validate_roles()
        return users

    def write(self, vals):
        guarded = {'baseer_access_role', 'group_ids', 'company_ids', 'role'}
        if guarded.intersection(vals):
            self._baseer_require_role_admin()
        if 'baseer_access_role' in vals and 'notification_type' not in vals:
            # The native notification preference is backed by a technical group.
            # Preserve it per user when replacing application grants, including bulk changes.
            with self.env.cr.savepoint():
                for user in self:
                    user.write(dict(vals, notification_type=user.sudo().notification_type))
            return True
        with self.env.cr.savepoint():
            result = super().write(self._baseer_role_values(vals))
            if guarded.intersection(vals):
                self._baseer_validate_roles()
        return result

    @api.constrains('baseer_access_role', 'group_ids')
    def _baseer_validate_roles(self):
        groups = self.env['res.groups']
        markers = groups.browse()
        for role in ROLE_NAMES:
            markers |= self.env.ref('baseer_access_roles.group_' + role, raise_if_not_found=False) or groups
        if len(markers) != 3:
            return
        optional = (self.env.ref('base.group_multi_company')
                    | self.env.ref('mail.group_mail_notification_type_inbox'))
        for user in self.sudo():
            actual = user.all_group_ids
            role = user.baseer_access_role
            expected = self.env.ref('baseer_access_roles.group_' + role) if role else groups
            if actual & markers != expected:
                raise ValidationError(self.env._('Assign Baseer roles using the Access Role selector.'))
            if role in ('accountant', 'cashier'):
                allowed = expected.all_implied_ids | optional
                if actual - allowed:
                    raise ValidationError(self.env._('These groups exceed the selected access role. Switch to manual access first.'))


class ResGroups(models.Model):
    _inherit = 'res.groups'

    @api.model
    def _baseer_validate_role_memberships(self):
        marked = self.env['res.users'].sudo().with_context(active_test=False).search([
            '|', ('baseer_access_role', '!=', False),
            ('all_group_ids', 'in', [g.id for name in ROLE_NAMES
             if (g := self.env.ref('baseer_access_roles.group_' + name, raise_if_not_found=False))]),
        ])
        marked._baseer_validate_roles()

    @api.model_create_multi
    def create(self, vals_list):
        security_change = any({'user_ids', 'all_user_ids', 'implied_ids', 'implied_by_ids'}.intersection(vals)
                              for vals in vals_list)
        if security_change:
            self.env['res.users']._baseer_require_role_admin()
        with self.env.cr.savepoint():
            groups = super().create(vals_list)
            if security_change:
                self._baseer_validate_role_memberships()
        return groups

    def write(self, vals):
        security_change = bool({'user_ids', 'all_user_ids', 'implied_ids', 'implied_by_ids'}.intersection(vals))
        if security_change:
            self.env['res.users']._baseer_require_role_admin()
        with self.env.cr.savepoint():
            result = super().write(vals)
            if security_change and 'baseer_access_role' in self.env['res.users']._fields:
                # Covers the inverse M2M path, which does not call res.users.write.
                self._baseer_validate_role_memberships()
        return result
