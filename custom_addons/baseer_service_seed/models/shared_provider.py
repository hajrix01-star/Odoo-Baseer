"""SP1 canonical providers and an explicit, transactional seed-only consolidation."""
import re

from odoo import _, api, Command, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL, html2plaintext

from .catalog import PROVIDERS


class Company(models.Model):
    _inherit = 'res.company'

    @api.model
    def _baseer_lock_shared_providers(self):
        # Serializes different companies as well as same-company retries. The
        # durable anchor UPDATE forces Repeatable Read callers with a stale
        # snapshot to receive 40001 and retry the entire native transaction.
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)', [1936747057, 1])
        self.env.cr.execute("""UPDATE ir_model_data SET id=id
            WHERE module='baseer_service_seed' AND name='tag_providers' RETURNING id""")
        if not self.env.cr.fetchone():
            raise ValidationError(_('Load the service provider catalog before preparing shared suppliers.'))

    @api.model
    def _baseer_provider_xmlid(self, name, record=None):
        data = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'baseer_service_seed'), ('name', '=', name)], limit=1)
        if data:
            if data.model != 'res.partner':
                raise ValidationError(_('The provider identity has the wrong model: %s', name))
            partner = self.env['res.partner'].sudo().with_context(active_test=False).browse(data.res_id).exists()
            if not partner:
                raise ValidationError(_('A referenced service provider was deleted: %s', name))
            if record and partner != record:
                raise ValidationError(_('The provider identity already belongs to another contact: %s', name))
            return partner
        if record:
            self.env['ir.model.data'].sudo().create({
                'module': 'baseer_service_seed', 'name': name, 'model': 'res.partner',
                'res_id': record.id, 'noupdate': True,
            })
            return record
        return self.env['res.partner']

    def _baseer_canonical_provider(self, provider):
        self.ensure_one()
        self._baseer_lock_shared_providers()
        key, arabic, aliases, tag_key, vat, default_leaf = provider
        name = f'provider_{key}'
        partner = self._baseer_provider_xmlid(name)
        if partner:
            if partner.company_id or partner.parent_id or partner.commercial_partner_id != partner:
                raise ValidationError(_('The canonical provider must remain a shared independent contact: %s', name))
            return partner
        # Build public catalog fields only: never copy a source's private
        # contact information, chatter, bank details, attachments or followers.
        partner = self.env['res.partner'].sudo().with_context(
            mail_create_nolog=True, mail_create_nosubscribe=True, tracking_disable=True,
        ).create({
            'baseer_name_ar': arabic, 'baseer_name_en': aliases.split('|')[0].strip(),
            'ref': aliases, 'company_id': False, 'country_id': self.env.ref('base.sa').id,
            'is_company': True, 'supplier_rank': 1, 'vat': vat or False,
            'category_id': [Command.set([
                self.env.ref('baseer_service_seed.tag_providers').id,
                self.env.ref(f'baseer_service_seed.tag_{tag_key}').id,
            ])],
        })
        return self._baseer_provider_xmlid(name, partner)

    @api.model
    def _baseer_provider_properties(self, partners):
        names = sorted(name for name, field in partners._fields.items()
                       if field.company_dependent and field.store and field.column_type)
        partners.flush_recordset(names)
        if not partners:
            return names, {}
        self.env.cr.execute(SQL('SELECT id, %s FROM res_partner WHERE id IN %s',
                                SQL(', ').join(SQL.identifier(name) for name in names), tuple(partners.ids)))
        return names, {row[0]: dict(zip(names, row[1:])) for row in self.env.cr.fetchall()}

    def _baseer_shared_service_provider(self, provider, mappings):
        self.ensure_one()
        self._baseer_lock_shared_providers()
        key, arabic, aliases, tag_key, vat, default_leaf = provider
        alias = f'provider_{key}_company_{self.id}'
        existing = self._baseer_provider_xmlid(alias)
        if existing and existing.company_id:
            if existing.company_id != self:
                raise ValidationError(_('The legacy provider belongs to another company: %s', alias))
            # Module upgrade alone is not authorization to migrate old sources.
            return existing
        partner = self._baseer_canonical_provider(provider)
        if existing and existing != partner:
            raise ValidationError(_('The company provider alias conflicts with the canonical provider: %s', alias))
        self._baseer_provider_xmlid(alias, partner)
        mapping = mappings.get(default_leaf) if default_leaf else None
        if mapping and mapping.active and partner.active:
            names, properties = self._baseer_provider_properties(partner)
            current = properties[partner.id]['baseer_purchase_category_map_id'] or {}
            # Explicit False is a deliberate company preference too.
            if str(self.id) not in current:
                partner.with_company(self).write({'baseer_purchase_category_map_id': mapping.id})
        return partner

    @api.model
    def _baseer_check_provider_sources(self, partners):
        """Reject business references before any canonical, alias or delete write."""
        if not partners:
            return
        partners.flush_recordset()
        ids = tuple(partners.ids)
        self.env.cr.execute('SELECT id FROM res_partner WHERE id IN %s ORDER BY id FOR UPDATE', [ids])
        self.env.cr.execute('UPDATE res_partner SET id=id WHERE id IN %s', [ids])
        self.env.cr.execute("""SELECT c.conrelid::regclass::text, a.attname
            FROM pg_constraint c JOIN pg_attribute a
            ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1]
            WHERE c.contype='f' AND c.confrelid='res_partner'::regclass
            AND cardinality(c.conkey)=1 ORDER BY 1,2""")
        for table, column in self.env.cr.fetchall():
            if table == 'res_partner_res_partner_category_rel':
                continue
            extra = SQL(' AND id != %s', SQL.identifier(column)) if (table, column) == ('res_partner', 'commercial_partner_id') else SQL('')
            self.env.cr.execute(SQL('SELECT %s FROM %s WHERE %s IN %s%s LIMIT 1',
                SQL.identifier(column), SQL.identifier(table), SQL.identifier(column), ids, extra))
            hit = self.env.cr.fetchone()
            if hit:
                raise ValidationError(_('Provider %s has a business reference in %s. Review it separately before consolidation.', hit[0], table))
        # Generic model/res_id links do not have a database foreign key.
        for model_name, model_class in self.env.registry.models.items():
            if not model_class._auto or model_name in ('mail.message', 'ir.model.data'):
                continue
            model = self.env[model_name].sudo()
            for field in model._fields.values():
                if not field.store or not field.column_type or field.inherited:
                    continue
                if field.type == 'many2one' and field.company_dependent and field.comodel_name == 'res.partner':
                    self.env.cr.execute(SQL("""SELECT id FROM %s WHERE EXISTS
                        (SELECT 1 FROM jsonb_each_text(COALESCE(%s, '{}'::jsonb)) p
                         WHERE p.value IN %s) LIMIT 1""",
                        SQL.identifier(model._table), SQL.identifier(field.name), tuple(str(pid) for pid in ids)))
                elif field.type == 'reference':
                    self.env.cr.execute(SQL('SELECT id FROM %s WHERE %s IN %s LIMIT 1',
                        SQL.identifier(model._table), SQL.identifier(field.name), tuple(f'res.partner,{pid}' for pid in ids)))
                elif field.type == 'many2one_reference':
                    owner = model._fields[field.model_field]
                    if not owner.store or not owner.column_type:
                        self.env.cr.execute(SQL('SELECT id FROM %s WHERE %s IN %s LIMIT 1',
                            SQL.identifier(model._table), SQL.identifier(field.name), ids))
                        if self.env.cr.fetchone():
                            raise ValidationError(_('Review the non-stored reference owner on %s before consolidation.', model_name))
                        continue
                    self.env.cr.execute(SQL('SELECT id FROM %s WHERE %s IN %s AND %s=%s LIMIT 1',
                        SQL.identifier(model._table), SQL.identifier(field.name), ids,
                        SQL.identifier(field.model_field), 'res.partner'))
                else:
                    continue
                if self.env.cr.fetchone():
                    raise ValidationError(_('A provider has linked documents in %s. Review them before consolidation.', model_name))
        for model_name, domain in (
            ('ir.attachment', [('res_model', '=', 'res.partner'), ('res_id', 'in', partners.ids)]),
            ('mail.followers', [('res_model', '=', 'res.partner'), ('res_id', 'in', partners.ids)]),
            ('mail.activity', [('res_model', '=', 'res.partner'), ('res_id', 'in', partners.ids)]),
        ):
            if self.env[model_name].sudo().search_count(domain, limit=1):
                raise ValidationError(_('A provider has private linked documents in %s.', model_name))
        languages = self.env['res.lang'].with_context(active_test=False).search([('code', 'in', ['en_US', 'ar_001', 'ar'])]).mapped('code')
        for partner in partners:
            if not partner.company_id or partner.parent_id or partner.commercial_partner_id != partner:
                raise ValidationError(_('Only independent private legacy providers can be consolidated: %s', partner.id))
            expected = {html2plaintext(str(partner.with_context(lang=lang)._creation_message())).strip() for lang in languages}
            messages = self.env['mail.message'].sudo().search([('model', '=', 'res.partner'), ('res_id', '=', partner.id)])
            if len(messages) > 1 or any(message.message_type != 'notification' or message.attachment_ids
                    or message.tracking_value_ids or html2plaintext(str(message.body)).strip() not in expected for message in messages):
                raise ValidationError(_('Provider %s has non-creation chatter. Review it separately before consolidation.', partner.id))

    @api.model
    def _baseer_consolidate_service_providers(self):
        """Explicit post-backup operation; no automatic install/upgrade migration."""
        self = self.sudo().with_context(active_test=False)
        with self.env.cr.savepoint():
            self._baseer_lock_shared_providers()
            catalog = {provider[0]: provider for provider in PROVIDERS}
            aliases = self.env['ir.model.data'].search([
                ('module', '=', 'baseer_service_seed'), ('model', '=', 'res.partner'),
                ('name', '=like', 'provider_%_company_%')])
            families = {}
            source_keys = {}
            for alias in aliases:
                match = re.fullmatch(r'provider_(.+)_company_(\d+)', alias.name)
                if not match or match[1] not in catalog:
                    continue
                company = self.browse(int(match[2])).exists()
                source = self.env['res.partner'].browse(alias.res_id).exists()
                if not company or not source or (source.company_id and source.company_id != company):
                    raise ValidationError(_('Invalid legacy service provider alias: %s', alias.name))
                if source.company_id:
                    if source_keys.setdefault(source.id, match[1]) != match[1]:
                        raise ValidationError(_('One legacy contact has several different provider identities.'))
                    expected = catalog[match[1]]
                    if source.name != expected[1] or (source.vat or '').strip() != (expected[4] or ''):
                        raise ValidationError(_('The legacy provider name or VAT differs from the approved catalog: %s', source.id))
                    families.setdefault(match[1], []).append((alias, source, company))
            sources = self.env['res.partner'].browse(sorted({source.id for rows in families.values() for alias, source, company in rows}))
            authorized_aliases = {alias.id for rows in families.values() for alias, source, company in rows}
            source_xmlids = self.env['ir.model.data'].search([('model', '=', 'res.partner'), ('res_id', 'in', sources.ids)])
            if any(data.id not in authorized_aliases for data in source_xmlids):
                raise ValidationError(_('A legacy provider has another external identity. Review it before consolidation.'))
            self._baseer_check_provider_sources(sources)
            property_names, properties = self._baseer_provider_properties(sources)
            result = []
            for key, rows in sorted(families.items()):
                scope = rows[0][2]
                canonical_before = self._baseer_provider_xmlid(f'provider_{key}')
                canonical = scope._baseer_canonical_provider(catalog[key])
                canonical_property_names, current = self._baseer_provider_properties(canonical)
                updates = {}
                for alias, source, company in rows:
                    for field_name in property_names:
                        for company_key, value in (properties[source.id][field_name] or {}).items():
                            target_company = self.browse(int(company_key)).exists()
                            if not target_company:
                                raise ValidationError(_('A legacy provider setting refers to a missing company.'))
                            field = canonical._fields[field_name]
                            if field.type == 'many2one' and value:
                                target = self.env[field.comodel_name].browse(value).exists()
                                if not target:
                                    raise ValidationError(_('A legacy provider setting refers to a deleted record: %s', field_name))
                                if field.check_company:
                                    domain = target._check_company_domain(target_company)
                                    if domain and target != target.with_context(active_test=False).filtered_domain(domain):
                                        raise ValidationError(_('A legacy provider setting belongs to another company: %s', field_name))
                            identity = (int(company_key), field_name)
                            existing = current[canonical.id][field_name] or {}
                            if identity in updates and updates[identity] != value:
                                raise ValidationError(_('Legacy providers contain conflicting company settings: %s', field_name))
                            if canonical_before and company_key in existing and existing[company_key] != value:
                                raise ValidationError(_('The canonical provider already has a different company setting: %s', field_name))
                            updates[identity] = value
                by_company = {}
                for (company_id, field_name), value in updates.items():
                    by_company.setdefault(company_id, {})[field_name] = value
                for company_id, values in by_company.items():
                    canonical.with_company(self.browse(company_id)).with_context(tracking_disable=True).write(values)
                for alias, source, company in rows:
                    alias.write({'res_id': canonical.id})
                    result.append({'source_id': source.id, 'canonical_id': canonical.id,
                                   'company_id': company.id, 'alias': alias.name, 'key': key})
            # All approved aliases now target the canonical records. Native
            # unlink handles only preflighted unused source contacts and their
            # known creation chatter; never use mass merge or SQL deletion.
            sources.with_context(tracking_disable=True).unlink()
            return {'deleted': len(sources), 'providers': len(families), 'lineage': result}
