# Baseer Company Accounting Setup

Additive Odoo19 companion for baseer_payroll. Native localization remains the chart authority. Independent companies receive missing salary, salary payable, deduction, employee advance and EOS expense settings, a general payroll journal, and an EOS purchase-journal link. Reuses exact compatible Saudi chart accounts before adding dedicated accounts. Starter sale/purchase/bank/cash journals are only added when absent.

New companies: select Saudi Arabia and SAR in the native company form. Native country onboarding loads its chart; the companion completes it after the native callback. A fully empty existing Saudi/SAR root can also be initialized at installation/update. Established/manual charts, currencies, configured references, existing accounts and archived customization are preserved. Unknown/uninitialized foreign companies wait for their native localization. Native branches retain shared accounting and are outside automatic payroll setup.

An empty manual outbound payment account is filled with native liquidity only if the journal has no moves/payments, the liquidity is asset_cash and not reconcilable. Used/configured journals retain their original matching workflow. This module never marks payments paid, issues invoices, creates balances or enables banking connectivity.

The private initializer runs on module installation/update and is idempotent. It never reloads an established chart. Invalid remembered seed identities/types require accounting review; they are not silently recreated. Administrative edits remain possible in native Payroll Settings and Accounting. Do not uninstall a seed owner after accounts/journals enter use; rollback uses a coherent database/source backup.

Arabic/English names for newly created seed records, native translated chart names and settings. No custom financial UI, new ledger, tax policy, business schema or third-party runtime dependency. QA acceptance evidence: docs/build-governance/COMPANY-ACCOUNTING-SEED.md in the parent repository.
