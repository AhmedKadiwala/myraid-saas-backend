# Company access and sales workflow

The active frontend starts at `frontend/src/reference/entry.js` and uses the
`/api/v1/workspace-ui/` endpoints. Its role labels now show the actual company
role; API authorization uses permissions rather than role-name aliases.

## Access and people

- `Tenant.owner` identifies the original owner. Existing companies use their
  earliest administrator membership when the migration identifies ownership.
- A co-owner has full company administration but cannot change the owner's
  identity, deactivate their membership or replace their role.
- Permissions describe single actions; roles group permissions; role-permission
  links describe the actions in a role; user-role assignments apply within one
  company. Admin lists explain these relationships and show readable people.
- Predefined global roles: Co-owner, Accountant, Sales, Marketing, Factory
  Manager, HR and Viewer. Company roles support descriptions and permission
  checkboxes. The Roles page shows the permitted actions in predefined roles.
- Employees/workers are workforce records. Creating one never creates a login.
  Names and valid phones are required for new workforce and contact records.
- People sign in with a unique username and password. Email is optional and is
  used for password recovery when supplied; phone is not a login requirement.

## Invitations

Select a role and username, then share the one-time setup link through WhatsApp
or another messaging app. The invitee chooses a password and may add an email
for recovery. Tokens expire after seven days; resend replaces the token; revoke
invalidates it. Active and pending invitations are checked against the company
seat limit. Role grants cannot exceed the inviter's permitted access.

Set `ERP_PUBLIC_APP_URL` to the frontend's public HTTPS origin, and configure
`WHATSAPP_API_URL` and `WHATSAPP_ACCESS_TOKEN` for the existing communications
gateway. Run the existing ERP communications/outbox worker to deliver queued
messages. Without a provider, invitations explicitly show **Provider not
connected** and remain pending. Configure the provider and resend; creating an
invitation alone never claims a WhatsApp message was sent.

## Orders and production

- Sales opportunity stages: New, Qualified, Quotation sent, Negotiation, Won,
  Lost. Creating a customer does not win an opportunity. Link a quotation/order
  to the opportunity; confirmation marks it Won.
- Quotations: Draft, Sent, Accepted, Rejected, Expired. Acceptance/conversion
  creates a confirmed sales order.
- Direct sales orders may be Draft or Confirmed. Confirmation creates one
  unassigned job automatically. The confirmation command is safe to retry.
- Pending jobs can be split into multiple jobs whose quantities total the order
  quantity. Assignment selects an active worker record and requires `job.assign`.
  Progress permission alone cannot change the assigned worker.
- Completing one job does not complete the remaining jobs. Readiness is based on
  the entire allocated quantity. Partial dispatch is limited to completed job
  quantities. An invoice alone does not finish production or shipping.
- Unstarted orders with no invoice or dispatch can be cancelled, retaining their
  job history. Rejected/expired quotations cannot become sales orders.
- Order PDFs can be uploaded at creation or later (10 MB each). Files use private
  storage and authenticated company-scoped downloads. Jobs access the same parent
  order's attachments.
- Inventory is hidden; receiving/dispatching does not require stock entries or
  deduct stock in this workspace. Products remain available under Sales. BOM and
  material-per-piece consumption remain deferred.

## Validation and rollout

Run `python manage.py migrate` for the owner, OTP phone, invitation, attachment,
predefined-role and initial-job migrations. Build the frontend with `npm run build`.
Publish the updated backend and frontend together; source edits alone do not
update the hosted frontend. Existing legacy multi-line/approval records keep
their read-only compatibility handling.

Automated coverage includes company boundaries, owner protection, phone-verified
invitation acceptance, custom roles, job splitting and assignment, partial
dispatch, cancellation, required phones and private PDFs. The browser workflow
test exercises the active login, products, orders, PDFs, jobs and team screens
against a disposable local company.

The proposed RBAC PDF has not been supplied; this implementation follows the
rules approved in the conversation. Its exact contents still need comparison
when the PDF is provided.
