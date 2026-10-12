# Connected ERP workspace

The primary interface now comes from `Sutriva_ERP_Django_Fullstack_Repository.zip`.
`apps/reference_erp/engine.py` retains that repository's command rules and its
GST-inclusive sales, procurement, stock, production, dispatch, finance, people,
attendance, payroll, approvals and task workflows. The 13 original engine tests
are included.

The integration uses existing `core.User`, `core.Tenant`, `TenantMembership`,
company roles, JWT/OTP authentication and subscription entitlements. It does not
create a second tenant or authentication database. Display names come from
`Tenant.name`; company edits update the tenant, ERP settings and tenant branding.

Run the usual deployment migrations:

```powershell
python manage.py migrate
```

The existing Render build already runs this command. New tables contain the
reference record payloads, audit events, workspace metadata and command receipts.
Commands lock the tenant row on PostgreSQL, run atomically and reject reuse of an
idempotency UUID with different input. Authorization is based on the real
membership, granular grants and current paid capabilities.

## Routes

Existing authentication and onboarding endpoints are retained. The frontend uses:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/workspace-ui/workspace/` | Authorized company state and branding |
| POST | `/api/v1/workspace-ui/commands/` | Reference form/action command |
| GET | `/api/v1/workspace-ui/records/<collection>/` | Authorized records |
| GET | `/api/v1/workspace-ui/records/<collection>/<code>/` | Authorized record |

Pass the existing bearer token and `X-Tenant-ID`. This interface is company-wide;
it rejects explicit branch filters. Role/plan previews cannot grant access.
Demo resets and subscription-changing commands are disabled.

## Existing data and cutover

On the first workspace load for each tenant, `importer.initialize` copies the
existing CRM customers/leads and supported normalized ERP records to the
reference schema inside a tenant transaction. Initialization is idempotent.
Original rows remain intact; no original tenant, identity or record is deleted.
The new workspace is authoritative for subsequent commands. The old normalized
ERP endpoints remain available for compatibility, but are **not synchronized**
with new workspace writes. Integrations should move to the workspace API before
using it as their operational ledger.

Each imported record keeps its source ID and model. Documents retain their full
original lines and totals. Documents with multiple lines or fractional quantities,
legacy payroll runs and legacy multi-step approvals are read-only in this simpler
reference interface. They must continue through the original workflow until a
full adapter is implemented. Supplier bills, credit notes, statutory accounting,
warehouse allocation and advanced legacy-only resources remain in their original
tables and are not represented as reference transactions.

The JSON fixture is fictional test data; it is never automatically loaded into
company workspaces. New companies start empty.

## Verification

```powershell
python manage.py test --settings=config.test_settings --noinput
python manage.py makemigrations --check --dry-run --settings=config.test_settings
```

84 tests passed, including company-name changes, cross-tenant isolation, actual
JWT authentication, invitations, role/feature denial, quote-to-invoice/payment,
stock receipt idempotency, imported-stock preservation, user limits and prevention
of invitation privilege escalation.

## Authentication failures

The first DRF authenticator now supplies an authentication challenge. Invalid or
expired JWTs therefore return 401 rather than being converted to 403. Actual
membership, role and feature denials remain 403. The authenticated user-profile
endpoint is independent of tenant selection and returns the user's active company
memberships, so an old tenant ID cannot prevent reading the user's own profile.
Public OTP endpoints ignore stale bearer/cookie/tenant state.

The full suite now passes 88 tests, including expired-token status, stale tenant
profile lookup, public OTP verification with stale headers and anonymous endpoint
protection.

## Negotiated monthly pricing and free trials

New company workspaces receive exactly seven days of free access, with trial
entitlements expiring at the same time. Onboarding validates chosen modules but
returns a negotiated quote with no fixed monthly price.

`Tenant.agreed_monthly_price` stores each company's privately agreed INR monthly
rate. The platform administrator can set it under Tenants in Django admin. It is
independent of shared subscription-plan pricing, so two companies with the same
capabilities can have different agreements. The workspace API returns only the
active tenant's agreed amount, or null while pricing is under discussion.

This records the agreement; it does not automatically infer willingness to pay,
change an existing payment-provider subscription, or collect a payment. Payment
collection can be connected to the agreed price when the billing module is built.

91 tests passed, including exact seven-day onboarding and independent agreed
prices for customers on the same plan.
