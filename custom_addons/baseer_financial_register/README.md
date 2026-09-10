# Baseer Financial Operations

Odoo 19 addon. Open **Invoicing → Financial operations** to inspect posted journal entries, invoices, credit notes, sales summaries and payments visible to the current user. The source button opens the existing source document with its normal permissions. Drafts and operations without a posted accounting entry are outside this register.

Native search, date filters, grouping, pagination, list export and document navigation remain available. Small screens use the native kanban view. The addon creates no transaction records and changes no posting or reconciliation behavior.

Invoice indicators are separated into customer and supplier sections, and by company currency. Credit notes subtract. Net invoices, settled amount, outstanding amount, partially paid document count and net overdue installments follow the current native accounting state. Settlements include payments, credit notes and write-offs; they are not a cash-flow total. Overdue includes only open installments whose maturity is before today, including signed credits. Document-date filters do not create a historical balance snapshot.

General journal entries, including sales summary postings and their collection entries, appear in the table but never inflate the invoice indicators. The generic entry amount is its native debit turnover, not revenue or net cash flow. Invoice rows display their document currency; indicators use company currency without combining different currencies.

Requires `account`, `baseer_access_roles` and `baseer_report_layout`. Existing accounting permissions, payroll privacy and company restrictions remain in force. The cashier preset cannot open the register or request its indicators; its existing POS permissions are unchanged. No users are automatically assigned permissions.

Verification uses isolated native accounting fixtures and browser checks. The bounded performance sample is documented in the private release evidence; it is not certification for larger installations.
