from django.urls import path
from .views import WorkspaceView, CommandsView, RecordsView
from .report_views import StatementView
from .platform_views import PlatformDashboardView, PlatformTenantsView, PlatformTenantDetailView, PlatformPaymentView, CompanyBillingView

urlpatterns = [
    path("workspace/", WorkspaceView.as_view()),
    path("commands/", CommandsView.as_view()),
    path("statement/", StatementView.as_view()),
    path("billing/", CompanyBillingView.as_view()),
    path("platform/", PlatformDashboardView.as_view()),
    path("platform/tenants/", PlatformTenantsView.as_view()),
    path("platform/tenants/<int:tenant_id>/", PlatformTenantDetailView.as_view()),
    path("platform/tenants/<int:tenant_id>/payments/", PlatformPaymentView.as_view()),
    path("records/<str:collection>/", RecordsView.as_view()),
    path("records/<str:collection>/<str:code>/", RecordsView.as_view()),
]
