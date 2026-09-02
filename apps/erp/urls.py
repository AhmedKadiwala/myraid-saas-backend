from django.urls import include, path
from rest_framework.routers import DefaultRouter
from . import views as v, workspace as w, documents as d, onboarding, otp

router = DefaultRouter()
for prefix, view in [
    ("items",v.ItemViewSet),("customers",v.CustomerViewSet),("suppliers",v.SupplierViewSet),
    ("warehouses",v.WarehouseViewSet),("bins",v.BinViewSet),("departments",v.DepartmentViewSet),
    ("cost-centers",v.CostCenterViewSet),("documents",v.DocumentViewSet),("jobs",v.JobViewSet),
    ("stock",v.StockViewSet),("stock-movements",v.MovementViewSet),("expense-categories",v.ExpenseCategoryViewSet),
    ("expenses",v.ExpenseViewSet),("recurring-expenses",v.RecurringViewSet),("payments",v.PaymentViewSet),
    ("employees",v.EmployeeViewSet),("shifts",v.ShiftViewSet),("holidays",v.HolidayViewSet),
    ("attendance",v.AttendanceViewSet),("leave-types",v.LeaveTypeViewSet),("leave-requests",v.LeaveViewSet),
    ("salary-components",v.SalaryComponentViewSet),("loans",v.LoanViewSet),("payroll",v.PayrollViewSet),
    ("tasks",v.TaskViewSet),("approvals",v.ApprovalViewSet),("approval-rules",v.ApprovalRuleViewSet),
    ("configurations",v.ConfigurationViewSet),("periods",v.PeriodViewSet),
    ("reservations",v.ReservationViewSet),("positions",v.PositionViewSet),("stock-counts",v.StockCountViewSet),
    ("communications",v.CommunicationViewSet),
    ("api-credentials",v.ApiCredentialViewSet),("webhooks",v.WebhookViewSet),
]: router.register(prefix,view,basename="erp-"+prefix)

urlpatterns = [
    path("onboard/",onboarding.OnboardView.as_view()),
    path("companies/",onboarding.AddCompanyView.as_view()),
    path("auth/otp/request-otp/",otp.RequestOTPView.as_view()),path("auth/otp/verify-otp/",otp.VerifyOTPView.as_view()),
    path("bootstrap/",w.BootstrapView.as_view()),path("dashboard/",w.DashboardView.as_view()),
    path("profitability/",w.ProfitabilityView.as_view()),path("facts/",w.FactsView.as_view()),
    path("calculate/",w.CalculationView.as_view()),path("settings/",w.SettingsView.as_view()),
    path("entitlements/",w.EntitlementView.as_view()),path("search/",w.SearchView.as_view()),
    path("ageing/",w.AgeingView.as_view()),
    path("export/",d.ExportView.as_view()),path("print/<str:resource>/<uuid:pk>/",d.PrintView.as_view()),
    path("attachments/",d.AttachmentView.as_view()),path("attachments/<uuid:pk>/download/",d.AttachmentDownloadView.as_view()),
    path("import-template/",d.ImportTemplateView.as_view()),path("imports/",d.ImportView.as_view()),path("imports/<uuid:pk>/commit/",d.ImportCommitView.as_view()),
    path("",include(router.urls)),
]
