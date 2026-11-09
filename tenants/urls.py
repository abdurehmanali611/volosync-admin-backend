from django.urls import path

from .branch_views import BranchPaymentRequestListView, BranchRequestListView
from .console_views import (
    OwnerCandidateView,
    StaffLoginToggleView,
    TenantAccessView,
    TenantBillingView,
    TenantModulesView,
    TenantReportingView,
    TenantOwnerLinkView,
    TenantWithoutOwnerView,
)
from .excel_views import ExcelImportView, ExcelTemplateView
from .platform_views import (
    AuditLogListView,
    FeedbackListView,
    MonthlySignupsView,
    SetupPaymentListView,
)
from .views import (
    AdminDashboardView,
    BranchCreateView,
    StaffListView,
    StaffResetPasswordView,
    SubscriptionListCreateView,
    TenantDetailView,
    TenantListView,
    TenantMeView,
    TenantOwnerView,
    TenantPaymentsView,
    TenantStaffView,
)

urlpatterns = [
    path("", TenantListView.as_view()),
    path("dashboard/", AdminDashboardView.as_view()),
    path("signups/", MonthlySignupsView.as_view()),
    path("subscriptions/", SubscriptionListCreateView.as_view()),
    path("staff/", StaffListView.as_view()),
    path("staff/<int:pk>/reset-password/", StaffResetPasswordView.as_view()),
    path("staff/<int:pk>/login/", StaffLoginToggleView.as_view()),
    path("feedback/", FeedbackListView.as_view()),
    path("audit/", AuditLogListView.as_view()),
    path("setup-payments/", SetupPaymentListView.as_view()),
    # Owner-filed branch approvals + multi-branch yearly payments.
    path("branch-requests/", BranchRequestListView.as_view()),
    path("branch-requests/<int:pk>/", BranchRequestListView.as_view()),
    path("branch-payments/", BranchPaymentRequestListView.as_view()),
    path("branch-payments/<int:pk>/", BranchPaymentRequestListView.as_view()),
    # Onboarding gaps: companies that still have no OWNER login.
    path("without-owner/", TenantWithoutOwnerView.as_view()),
    path("<int:pk>/", TenantDetailView.as_view()),
    path("<int:pk>/payments/", TenantPaymentsView.as_view()),
    # Console operations (access, billing, modules, reporting).
    path("<int:pk>/access/", TenantAccessView.as_view()),
    path("<int:pk>/billing/", TenantBillingView.as_view()),
    path("<int:pk>/modules/", TenantModulesView.as_view()),
    path("<int:pk>/reporting/", TenantReportingView.as_view()),
    path("<int:pk>/staff/", TenantStaffView.as_view()),
    path("<int:pk>/owner/", TenantOwnerView.as_view()),
    path("<int:pk>/owner/candidates/", OwnerCandidateView.as_view()),
    path("<int:pk>/owner/link/", TenantOwnerLinkView.as_view()),
    path("<int:pk>/branches/", BranchCreateView.as_view()),
    # Excel seeding: blank template download, then row-by-row import.
    path("<int:pk>/excel/template/", ExcelTemplateView.as_view()),
    path("<int:pk>/excel/import/", ExcelImportView.as_view()),
    path("me/", TenantMeView.as_view()),
]
