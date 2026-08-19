from django.urls import path

from . import auth_views, crm_views, rbac_views, tenant_views

urlpatterns = [
    # Authentication / Simple JWT
    path("auth/login", auth_views.LoginView.as_view()),
    path("auth/refresh", auth_views.RefreshView.as_view()),
    path("auth/logout", auth_views.LogoutView.as_view()),
    path("auth/signup", auth_views.SignupView.as_view()),
    path("auth/user-info", auth_views.UserInfoView.as_view()),
    path("auth/edit-user/<int:user_id>", auth_views.EditUserView.as_view()),
    path("auth/change-password", auth_views.ChangePasswordView.as_view()),
    path("auth/reset-password/<int:user_id>", auth_views.ResetPasswordView.as_view()),

    # Tenant SaaS surface
    path("tenants/me", tenant_views.CurrentTenantView.as_view()),
    path("tenants/settings", tenant_views.TenantSettingsView.as_view()),
    path("tenants/branches", tenant_views.BranchListCreateView.as_view()),
    path("plans", tenant_views.PlanListView.as_view()),
    path("billing/subscription", tenant_views.SubscriptionView.as_view()),
    path("billing/invoices", tenant_views.InvoiceListView.as_view()),
    path("billing/razorpay/webhook", tenant_views.RazorpayWebhookView.as_view()),

    # Dynamic RBAC
    path("rbac/permissions", rbac_views.PermissionListView.as_view()),
    path("rbac/roles", rbac_views.RoleListCreateView.as_view()),
    path("rbac/roles/<int:role_id>", rbac_views.RoleDetailView.as_view()),
    path("rbac/assignments", rbac_views.UserRoleListCreateView.as_view()),
    path(
        "rbac/assignments/<int:assignment_id>",
        rbac_views.UserRoleDetailView.as_view(),
    ),
    path("rbac/effective-permissions", rbac_views.EffectivePermissionView.as_view()),
    path("audit-logs", rbac_views.AuditLogListView.as_view()),
    # Compatibility alias; content now represents atomic business permissions.
    path("permissions/get-all", rbac_views.PermissionListView.as_view()),

    # Employees and lookups
    path("employees/get-sales", crm_views.SalesEmployeeListView.as_view()),
    path("employees/get-all", crm_views.EmployeeListView.as_view()),
    path(
        "employees/get-assigned/<str:ref_id>",
        crm_views.AssignedEmployeeView.as_view(),
    ),
    path("sources/get", crm_views.SourceView.as_view()),
    path("sources/add", crm_views.SourceView.as_view()),
    path("sources/edit/<int:pk>", crm_views.SourceView.as_view()),
    path("products/get", crm_views.ProductView.as_view()),
    path("products/add", crm_views.ProductView.as_view()),
    path("products/edit/<int:pk>", crm_views.ProductView.as_view()),

    # Companies / clients
    path("company/get", crm_views.CompanyListView.as_view()),
    path("company/get-client/<int:company_id>", crm_views.CompanyClientView.as_view()),
    path("company/add/client/<int:company_id>", crm_views.CompanyClientView.as_view()),
    path("company/edit/client/<int:company_id>", crm_views.CompanyClientView.as_view()),
    path(
        "company/edit/company-details/<int:company_id>",
        crm_views.CompanyDetailView.as_view(),
    ),

    # Leads
    path("leads/get", crm_views.LeadListCreateView.as_view()),
    path("leads/add", crm_views.LeadListCreateView.as_view()),
    path("leads/get/<int:lead_id>", crm_views.LeadDetailView.as_view()),
    path("leads/edit/<int:lead_id>", crm_views.LeadDetailView.as_view()),
    path("leads/getBy/<str:duration>", crm_views.LeadAnalyticsView.as_view()),

    # Deals
    path("deals/get", crm_views.DealListCreateView.as_view()),
    path("deals/add", crm_views.DealListCreateView.as_view()),
    path("deals/get-only-id", crm_views.DealIdsView.as_view()),
    path("deals/get/<str:deal_id>", crm_views.DealDetailView.as_view()),
    path("deals/edit/<str:deal_id>", crm_views.DealDetailView.as_view()),
    path("deals/edit/status/<str:deal_id>", crm_views.DealStatusView.as_view()),
    path("deals/convert/<int:lead_id>", crm_views.ConvertLeadView.as_view()),
    path("deals/getBy/<str:duration>", crm_views.DealAnalyticsView.as_view()),

    # Descriptions and reminders
    path("descriptions/get/<str:ref_id>", crm_views.DescriptionView.as_view()),
    path("descriptions/add/<str:ref_id>", crm_views.DescriptionView.as_view()),
    path("descriptions/edit/<int:ref_id>", crm_views.DescriptionView.as_view()),
    path("descriptions/delete/<int:ref_id>", crm_views.DescriptionView.as_view()),
    path("reminders/get/<str:ref_id>", crm_views.ReminderView.as_view()),
    path("reminders/add/<str:ref_id>", crm_views.ReminderView.as_view()),
    path("reminders/edit/<int:ref_id>", crm_views.ReminderView.as_view()),
    path("reminders/delete/<int:ref_id>", crm_views.ReminderView.as_view()),
    path("reminders/get-by-month/<str:month>", crm_views.ReminderMonthView.as_view()),

    # Notifications
    path("notifications/get-unread", crm_views.NotificationListView.as_view()),
    path("notifications/get-read", crm_views.ReadNotificationListView.as_view()),
    path(
        "notifications/mark-read/<int:notification_id>",
        crm_views.MarkNotificationView.as_view(),
    ),
    path("notifications/mark-all-read", crm_views.MarkNotificationView.as_view()),

    # Quotations
    path("quotations/get-products", crm_views.QuotationProductsView.as_view()),
    path(
        "quotations/add/<str:deal_id>",
        crm_views.QuotationListCreateView.as_view(),
    ),
    path("quotations/get-all", crm_views.QuotationListCreateView.as_view()),
    path("quotations/compactor", crm_views.CompactorView.as_view()),
    path(
        "quotations/get-by/<str:deal_id>",
        crm_views.QuotationByDealView.as_view(),
    ),
    path(
        "quotations/get/<int:quotation_id>",
        crm_views.QuotationDetailView.as_view(),
    ),
    path(
        "quotations/edit/<str:deal_id>/<int:quotation_id>",
        crm_views.QuotationDetailView.as_view(),
    ),
    path(
        "quotations/import/<int:quotation_id>",
        crm_views.QuotationImportView.as_view(),
    ),
    path(
        "quotations/delete/<int:quotation_id>",
        crm_views.QuotationDetailView.as_view(),
    ),
    path(
        "quotations/by-quotation-no/<str:quotation_no>",
        crm_views.QuotationNumberView.as_view(),
    ),

    # Orders
    path("orders/add", crm_views.OrderListCreateView.as_view()),
    path("orders/get", crm_views.OrderListCreateView.as_view()),
    path("orders/get/<int:order_id>", crm_views.OrderDetailView.as_view()),
    path("orders/edit/<int:order_id>", crm_views.OrderDetailView.as_view()),
    path("orders/delete/<int:order_id>", crm_views.OrderDetailView.as_view()),
    path("orders/add/colour/<int:order_id>", crm_views.OrderColourView.as_view()),
    path("orders/add/payment/<int:order_id>", crm_views.OrderPaymentView.as_view()),
    path(
        "orders/edit/payment/<int:payment_id>",
        crm_views.OrderPaymentView.as_view(),
    ),
    path(
        "orders/delete/payment/<int:payment_id>",
        crm_views.OrderPaymentView.as_view(),
    ),

    # Drawings / files
    path("drawings/get-uploadUrl", crm_views.DrawingUploadUrlView.as_view()),
    path("drawings/upload", crm_views.DrawingCreateView.as_view()),
    path("drawings/get-all", crm_views.DrawingListView.as_view()),
    path("drawings/get/<str:ref_id>", crm_views.DrawingListView.as_view()),
    path("drawings/view/<int:drawing_id>", crm_views.DrawingDetailView.as_view()),
    path("drawings/delete/<int:drawing_id>", crm_views.DrawingDetailView.as_view()),
    path("drawings/approve/<int:drawing_id>", crm_views.ApproveDrawingView.as_view()),
    path("drawings/reject/<int:drawing_id>", crm_views.RejectDrawingView.as_view()),
    path(
        "drawings/show-in-order/<int:drawing_id>",
        crm_views.DrawingShowInOrderView.as_view(),
    ),
]
