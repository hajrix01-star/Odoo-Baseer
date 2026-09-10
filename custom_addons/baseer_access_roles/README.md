# Baseer ready access roles

In Settings > Users > Access Rights, an administrator selects **Baseer Access Role**.
Presets replace existing explicit groups atomically; existing users are not reassigned.
Clearing the preset starts manual configuration with internal-user access.

* Owner / General Manager: the explicit installed application manager inventory in
  `models/role_seed.py`, Settings, and all current companies. Newly created companies
  are added to owners. Native accounting/company invariants still apply.
* Accountant: Invoicing, Purchase, Sales and POS. Approves purchase batches using the
  existing native approval flow. No unrelated HR, CRM or project business reads.
* Cashier: POS and own bulk purchase batches. Draft input only; accountant approval.
  Own approved summaries remain readable without general accounting access.

Limited roles retain required shared product/contact/tax access. Menu filtering is
supplementary to native ACLs and conditional global rules. The optional model/group
inventory is explicit and installation/update is idempotent. New unrelated modules
require extending and reviewing that inventory. There is no arbitrary sudo proxy.

The addon contains no employee data, passwords or existing-user assignments.

## Restricted employee advances

Owner, accountant and cashier presets can use **Employee Advances** under POS;
accountants and owners also have an Invoicing entry point. A cashier sees only
their own advance entries, while accountants see entries in allowed companies.
Both limited roles can approve and record a disbursement. This does not allow
cashiers to approve purchase invoice batches.

The entry form exposes the public employee name, amount, dates, installments,
cash/bank journal, safe advance reference and native outstanding balance. It
does not expose private employee records, salary, payroll details, raw loan or
journal-entry navigation, repayment actions, chatter or correction actions.
Disbursement reuses the native payroll loan implementation, including account
configuration, exact installments, lock dates and posting checks. An issued
entry is immutable, and repeated approval does not post again.

The narrowly scoped payroll capability is an in-process object identity accepted
only under sudo on the exact `baseer.hr.loan` model. Client context flags cannot
supply it. Company/ownership/employee/journal checks precede elevation; only
fixed input fields pass to native loan creation. Loan and entry changes share
the transaction and entry row lock. The retained current user identifies who
recorded the advance. No external bank transfer is sent.

Before uninstalling, an administrator must clear assigned presets and configure
the users' replacement manual access. Uninstall removes the role groups and their
grants; it keeps user identities and business records. Keep a separate existing
administrative account until replacement access has been verified.
