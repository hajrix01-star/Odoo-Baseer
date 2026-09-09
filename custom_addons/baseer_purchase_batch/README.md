# Baseer purchase batches

This QA feature records up to 50 summarized supplier invoices in one native Odoo form. It creates native vendor bills and, for paid rows, native payments. It does not receive inventory.

## Configuration

An accounting manager opens **Vendors → Purchase Category Mappings** and links each company/category to an existing service product in that category. The product must already have a suitable expense account through normal Odoo configuration. No products, categories or financial accounts are created silently.

Paid entries require an existing outgoing bank/cash payment method that can immediately settle the invoice. Enable the explicit Credit switch to pay later; it clears and disables the payment method. With Credit off, a payment method is required. The feature does not modify bank or payment settings.

Suppliers must be active shared contacts or contacts assigned to the batch company; their commercial parent must also be shared or belong to that company. A new supplier is eligible even before its first bill. Accounting managers can set Default Purchase Category in the supplier's native Sales & Purchase tab, including shared suppliers. The category property is independent for each active company and its choices remain company-specific. Selecting that supplier suggests the current company's configured category; the operator can change it for the invoice. Native payable accounts and other accounting properties keep their existing company-dependent behavior.

## Entry

1. Select the active company in the Odoo header, then open **Vendors → Purchase Batches**. The list is scoped to that company and new batches inherit it. Its name stays visible and read-only; enter the administrative entry date. A draft opened under a different active company must be edited after switching the header back to its company.
2. Add invoice rows: existing shared or company supplier, invoice date, supplier reference, type, mapped category, gross amount, VAT 15% switch, Credit switch and payment method. Enable VAT to use the company's configured standard purchase tax; disable it for no tax. Credit defaults off, requiring cash or bank; enable it explicitly for a payable invoice.
3. Description, computed net/tax and original bill links are available as optional columns and in the full row form.
4. Normal **Save** keeps a draft. **Save and approve** validates all rows and creates their native accounting documents in one transaction. Payment date equals each row's invoice date, independently of the batch entry date.
5. Open **Vendor Bills** or a row's original document link to inspect the resulting native records. Print the batch summary from **Print Summary** or the native Print menu.

Approval fails as a whole if a row is invalid; it must not leave partial documents. Approved batch inputs remain immutable. Correct financial documents through Odoo's native reversal/credit workflow.

## Mobile and language

The same native one2many uses an editable list on desktop and invoice cards on small screens. Tap a card to open all fields in the row form. English and Arabic labels use Odoo translations and native RTL/LTR controls. There is no additional frontend framework or JavaScript calculation.

The desktop batch form uses a wider, scoped container with fixed native column widths: supplier first, then date, with a compact category column. Text follows the interface direction, dates are centered and monetary values use aligned Western digits. The fixed SAR currency remains on monetary amounts without a separate currency selector. Phone cards group supplier/amount, date/reference, category and explicit credit/payment/tax status; Add invoice opens the native row form. Intermediate widths retain Odoo's native table scrolling.

Existing rows with a different or historical tax show the actual tax name read-only instead of the 15% switch. A batch containing these rows also shows an Applied Tax column. No existing invoice tax is rewritten by the presentation change.

The batch date fields extend Odoo's native date widget only to display Western digits (0–9) in both languages. Dates, calendar selection, validation and persistence retain their native behavior; no global locale settings are changed.

## Print

The A4 landscape summary groups invoice date, supplier/reference, category/description, payment and gross/tax/net into seven columns. Money strings come from the backend; QWeb performs no financial arithmetic. The report reuses Baseer's IBM Plex Arabic font asset.

Installation and acceptance in the main database are outside this QA delivery.

Category labels use the shared `baseer_category_display` addon. Entry fields show the leaf name; **Search More** shows a separate parent category column to distinguish identical names. Full category paths remain available to explicit hierarchical views and native invoice description defaults.
