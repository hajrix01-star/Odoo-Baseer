# Baseer Common Services Seed (CSS1 / SP1)

Odoo 19 reusable, fill-only company setup. Depends on `baseer_company_setup`
and `baseer_purchase_batch`; neither dependency nor native code is modified.

## Result

* 20 shared canonical supplier contacts, 26 company-private service products,
  one native purchase-category mapping per selectable leaf.
* Shared Arabic category hierarchy and translated supplier tags, owned by
  stable XML IDs under `noupdate`. Existing categories are not renamed/moved.
* Expense accounts reuse valid native Saudi XML identities. Missing purposes
  receive a collision-safe dedicated expense account, shared by related services.
* Existing purchase/bank/cash journals remain authoritative; no service journals.
* Three published VAT values only: Saudi Energy, stc and Mobily. Other VAT fields
  are unknown/blank, not declarations of exemption. No default product taxes.
* Government entities and platforms have no forced supplier expense category.
  GOSI and ZATCA have contacts/tags only, not payment products or default expenses.

Company creation/chart-loading callbacks extend CAS1 after its native setup.
Module data initializes existing independent SAR companies with an existing chart.
Non-SAR companies are skipped because the current batch module requires SAR;
their country, currency and native accounting setup are not changed by CSS1.
Branches inherit native parent accounting policy and are not separately seeded.
The global advisory lock and durable catalog-identity MVCC anchor serialize
canonical creation across companies; company row locks protect company masters.
Stale Repeatable Read transactions must retry the whole native transaction;
no commits occur inside the seed. A transaction failure rolls back the seed.

`res.company._baseer_seed_services()` prepares a recordset; the model method
`_baseer_initialize_services()` prepares all eligible companies. Stable identities
are `<kind>_<key>_company_<id>` for `product`, `mapping` and `account`.
Canonical suppliers use `provider_<key>`; `provider_<key>_company_<id>` remains
a compatible company alias. Catalog constants are `PROVIDERS`, `SERVICES`,
`ACCOUNT_PURPOSES`.

Existing identity-owned records (including archived or renamed choices) are
preserved. Existing company/category mappings win. No arbitrary existing contact
is adopted by name or VAT. A missing identity target raises instead of silently
recreating a deleted record. Canonical records retain manual names, archive state,
VAT and settings. Category suggestions fill only an absent company property key;
an explicit empty value is also preserved. Accounting properties remain native
company-dependent fields and never become global financial settings.

## Explicit legacy consolidation

Updating the module does **not** consolidate existing private aliases. After a
verified backup and reference inventory, the private administrative method
`env['res.company']._baseer_consolidate_service_providers()` performs one atomic
operation and returns source/canonical/company/alias lineage. Repeating it is a
no-op once all aliases point to canonical contacts. There is no internal commit.
The returned `deleted`, `providers` and `lineage` evidence must be saved alongside
the verified backup before delivery; deleted IDs are not retained as live XMLIDs.

Only exact catalog company aliases participate. Business foreign keys, generic
document references, attachments, followers, activities and non-creation chatter
block consolidation. Unexpected external identities or a changed catalog name/VAT
also block. The user's explicit deletion instruction permits removal of an unused
source and its one native creation message. New canonical cards contain only vetted catalog
public data, never source addresses, bank data, messages or files. Independent
private sources are deleted through native ORM unlink after all property copying
and compatibility alias retargeting succeeds. Existing invoices, payments,
employee contacts and other historical business references are never rewritten.

Explicit company-dependent properties are copied via native per-company writes.
Missing targets, wrong-company relational settings, conflicting sources or an
existing canonical setting with another value abort the transaction. This method
does not use Odoo's general partner-merge wizard and is intended for the approved
unused catalog sources only; referenced suppliers need a separate reviewed plan.

## Rollback

Restore the verified database backup taken before installation and retain the
matching existing filestore when rolling back this seed. CSS1 creates no
attachments. **Do not use module uninstall as a rollback method.**
Stable XML IDs also identify adopted pre-existing suppliers or mappings. Odoo's
native uninstall treats these references as module-owned data and can attempt to
delete their records, including adopted masters that have no blocking references.
Uninstall is therefore not a preservation-safe reversal of this additive setup.

## Input and language behavior

Selecting a seeded leaf (including a supplier's default leaf) suggests Expense
on a new purchase-batch form; the user may change the type afterward. Backend
creation preserves any explicitly supplied `entry_type`, and only defaults a
missing type. Existing lines are never migrated or rewritten by setup.

Odoo category and contact names are not translated native fields. Categories use
Arabic names. New canonical contacts use `Arabic | English`, with available
vetted English names and additional aliases retained in native `ref`.
Optional supplier fields `baseer_name_ar` and `baseer_name_en` are editable on the
native contact card. Explicitly changing either recomposes `name`; ordinary
contacts and direct `name` edits remain native. Repeated setup never renames an
existing canonical contact or fills bilingual fields on historical sources.
Products use native translated names (English and active Arabic languages), and
supplier tags use native Arabic translation. No field schema is changed globally.

## Boundaries

This is master-data preparation, not HR service issuance/renewal, bills, payments,
payroll deductions, asset prepayments or amortization. Insurance products denote
expense classification only; posting an annual policy needs its actual accounting
treatment. Provider brands do not determine the legal invoice issuer or tax rate.
Platform subscription products do not represent every license/visa fee paid via
that platform. No final-exit product is provided. One Visas leaf/product is used;
later HR detail may distinguish issue/extension of exit-and-return.

Approved catalog/account/VAT evidence: repository documents
`COMMON-SERVICES-SEED.md`, `COMMON-SERVICES-SEED-GATE-REVIEW.md`, and
`COMMON-SERVICE-PARTIES-SEED-DECISIONS.md` dated 2026-09-08.
