from django.urls import path
from .views import WorkspaceView, CommandsView, RecordsView
from .report_views import StatementView
from .team import RolesView, InvitationsView, InvitationAcceptView, MemberRoleView
from .attachments import OrderAttachmentsView, OrderAttachmentDownloadView, DocumentPDFView, LocalFallbackDownloadView
from .platform_views import (
    PlatformDashboardView, PlatformTenantsView, PlatformTenantDetailView,
    PlatformPaymentView, CompanyBillingView, SalesRepDashboardView, SalesRepInviteCompanyView
)

urlpatterns = [
    path("roles/", RolesView.as_view()),
    path("invitations/", InvitationsView.as_view()),
    path("invitations/accept/", InvitationAcceptView.as_view()),
    path("members/<int:membership_id>/role/", MemberRoleView.as_view()),
    path("orders/<str:order_code>/attachments/", OrderAttachmentsView.as_view()),
    path("attachments/<int:pk>/download/", OrderAttachmentDownloadView.as_view()),
    path("documents/<str:doc_type>/<str:doc_code>/pdf/", DocumentPDFView.as_view()),
    path("documents/download-key/", LocalFallbackDownloadView.as_view()),
    path("workspace/", WorkspaceView.as_view()),
    path("commands/", CommandsView.as_view()),
    path("statement/", StatementView.as_view()),
    path("billing/", CompanyBillingView.as_view()),
    path("platform/", PlatformDashboardView.as_view()),
    path("platform/tenants/", PlatformTenantsView.as_view()),
    path("platform/tenants/<int:tenant_id>/", PlatformTenantDetailView.as_view()),
    path("platform/tenants/<int:tenant_id>/payments/", PlatformPaymentView.as_view()),
    path("platform/sales-rep/metrics/", SalesRepDashboardView.as_view()),
    path("platform/sales-rep/invite-company/", SalesRepInviteCompanyView.as_view()),
    path("records/<str:collection>/", RecordsView.as_view()),
    path("records/<str:collection>/<str:code>/", RecordsView.as_view()),
]
