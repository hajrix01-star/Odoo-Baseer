# Employee Services

Employee service records reuse the approved service catalog, supplier bills and native Odoo payments. No new payment ledger is introduced.

## Workflow

1. Open **Employees → Employee Services** or the **Employee Services** page in an employee record.
2. Enter the service, provider, invoice date and total cost. Enable VAT only when it applies. Attach supporting documents in the chatter.
3. Save a draft. An HR manager with the required accounting permissions approves it to issue one posted supplier bill.
4. Use **Register Payment** for native partial or full payments and **Open Bill** for accounting details.
5. Reprint the service statement from the record at any time. It describes the service and current payment status; it is not a final release or proof of cash receipt.

The general register supports employee, service, provider, month, expiry and status filters. The employee financial page retains its existing payroll manager restriction. The separate services page uses the existing HR permissions.

## Scope and access

- Current company and SAR currency; employee and service mapping belong to that company. Providers may be shared or belong to the current company. Proposed providers use the shared seed identity, falling back to the legacy company alias. Native accounting properties remain company-specific.
- Fifteen employee service types from `baseer_service_seed`, including visa issue/extension.
- HR users manage drafts. Financial operations require native accounting access; no accounting permissions are granted by this addon.
- Unbilled draft or canceled records may be deleted. An issued service can be canceled only after its bill is canceled or fully reversed; its linked history cannot be deleted. Archive records to retain history. Posted bill corrections use native credit notes.
- Payment status follows the native invoice. In this Community configuration, `paid` does not independently confirm that a bank transfer has been matched.
- Company-paid costs only: no automatic payroll deduction, loan or prepaid expense amortization.

## Interface and reports

Native Odoo form, list, search, kanban, chatter, monetary fields and the existing `baseer_latin_date` widget. Mobile cards and logical Bootstrap spacing support both Arabic and English. No JavaScript, added UI library or global CSS. PDF styling is scoped to the A4 service statement and reuses `baseer_report_layout` fonts. Amounts and report formatting come from the backend.

Runtime acceptance is tracked in `docs/build-governance/HR-SERVICES-BUILD.md`; static checks alone do not certify installation, access rules or PDF pagination.
