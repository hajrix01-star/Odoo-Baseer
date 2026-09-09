"""Optional bilingual supplier names; ordinary native contact editing remains intact."""
from odoo import api, fields, models
from odoo import _
from odoo.exceptions import ValidationError


def compose_names(arabic, english):
    return ' | '.join(value.strip() for value in (arabic, english) if value and value.strip())


def legacy_name_parts(name):
    """Classify supplied text only; never translate or guess a legal name."""
    name = (name or '').strip()
    parts = name.split(' | ')
    if (len(parts) == 2 and all(parts)
            and any('\u0600' <= char <= '\u06ff' for char in parts[0])
            and not any('\u0600' <= char <= '\u06ff' for char in parts[1])):
        return dict(zip(('baseer_name_ar', 'baseer_name_en'), parts))
    key = 'baseer_name_ar' if any('\u0600' <= char <= '\u06ff' for char in name) else 'baseer_name_en'
    return {'baseer_name_ar': False, 'baseer_name_en': False, key: name or False}


class Partner(models.Model):
    _inherit = 'res.partner'

    baseer_name_ar = fields.Char(string='Arabic Name', copy=False)
    baseer_name_en = fields.Char(string='English Name', copy=False)

    @api.onchange('baseer_name_ar', 'baseer_name_en')
    def _onchange_baseer_names(self):
        for record in self:
            name = compose_names(record.baseer_name_ar, record.baseer_name_en)
            if name:
                record.name = name

    @api.model_create_multi
    def create(self, vals_list):
        clean = []
        for values in vals_list:
            values = dict(values)
            if {'baseer_name_ar', 'baseer_name_en'} & values.keys():
                name = compose_names(values.get('baseer_name_ar'), values.get('baseer_name_en'))
                if name:
                    values['name'] = name
            clean.append(values)
        return super().create(clean)

    def write(self, values):
        name_keys = {'baseer_name_ar', 'baseer_name_en'}
        if not name_keys & values.keys() and 'name' not in values:
            return super().write(values)
        for record in self:
            changed = dict(values)
            if not name_keys & changed.keys():
                # Native/API renames remain supported without stale hidden components.
                if (record.baseer_name_ar or record.baseer_name_en) and changed['name'] != record.name:
                    changed.update(legacy_name_parts(changed['name']))
            if name_keys & changed.keys():
                name = compose_names(changed.get('baseer_name_ar', record.baseer_name_ar),
                                     changed.get('baseer_name_en', record.baseer_name_en))
                if not name:
                    raise ValidationError(_('Enter at least one Arabic or English name.'))
                if name != record.name:
                    changed['name'] = name
                else:
                    changed.pop('name', None)  # Filling language fields must not rewrite native documents.
            super(Partner, record).write(changed)
        return True

    def _baseer_initialize_name_parts(self):
        for partner in self:
            if partner.name and not (partner.baseer_name_ar or partner.baseer_name_en):
                parts = legacy_name_parts(partner.name)
                # Existing printed/native names must not change during upgrade.
                if compose_names(parts['baseer_name_ar'], parts['baseer_name_en']) == partner.name:
                    partner.write(parts)
