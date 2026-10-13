# Owner login and sample company

Phone OTP creates a persistent session with a minimum **31-day refresh lifetime**.
The 15-minute access token renews automatically, including after closing and
reopening the browser. Each successful refresh starts another 31-day period.
Explicit logout, disabled identities, token revocation, cleared browser storage,
or more than 31 days without renewal require OTP again. Existing seven-day
refresh tokens retain their old expiry; sign in once after this release to receive
the longer session. Cookies use the same expiry as their tokens.

One phone identifies one person across companies. The company selector switches
the tenant boundary. Only a platform super admin can add another company, using
the owner's existing phone in the platform company form. Company owners and
ordinary staff receive 403 from the add-company API. Self-registration creates
only the first company for an unused phone.

Every available module is free for the company's first seven days. Trial module
access respects staff permissions and ends at the trial expiry. Company owners
can still open billing while signed in after operational access expires. GST
gateway integrations remain outside the implemented V1 modules.

## Separate demo owner

Explicitly create the fictional sample company in the configured database:

```powershell
python manage.py seed_owner_demo
```

Sign in at the application URL with **9999900002**. In the current mock OTP
environment, use the six-digit developer code shown after requesting it. When
SMS mode is enabled, use an unused phone you control with `--phone` instead.
The demo user has company-owner rights and no platform-admin rights.

The company is **Meridian Seals · Demo**, slug `myraid-owner-demo`, with 56
fictional records: customers, leads, items, suppliers, quotes, sales orders,
jobs, purchases, dispatches, invoices, receipts, expenses, an outgoing payment,
employees, attendance, payroll, approvals, tasks, and stock movements. Its
dates are relative to the seed date; its full trial lasts seven days.
Rerunning the command keeps all test edits and the original trial expiry.
It refuses to overwrite a real company or reuse an existing phone identity.

Try these connected flows:

1. Receive pending purchase PO-001 and check the inventory increase.
2. Convert Q-002 to an order, create/progress its job, dispatch it, and invoice it.
3. Record a partial invoice payment and compare receivables and cash receipts.
4. Add an expense and settle it through an outgoing payment.
5. Add attendance, generate/finalize payroll for a new month, and record salary settlement.
6. Approve a pending request, complete a task, and inspect the audit trail.
7. Open reports and export a financial statement for the current month.

Automated checks cover the complete new-company operational workflow as well
as login refresh after 30 days, rotation/logout revocation, company isolation,
restricted provisioning, full trials and retained billing access after expiry.
