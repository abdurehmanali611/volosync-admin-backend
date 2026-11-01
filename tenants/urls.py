from django.urls import path

from .platform_views import (
    AuditLogListView,
    FeedbackListView,
    ModuleRequestListView,
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
    path("feedback/", FeedbackListView.as_view()),
    path("audit/", AuditLogListView.as_view()),
    path("setup-payments/", SetupPaymentListView.as_view()),
    path("module-requests/", ModuleRequestListView.as_view()),
    path("<int:pk>/", TenantDetailView.as_view()),
    path("<int:pk>/payments/", TenantPaymentsView.as_view()),
    path("<int:pk>/staff/", TenantStaffView.as_view()),
    path("<int:pk>/owner/", TenantOwnerView.as_view()),
    path("<int:pk>/branches/", BranchCreateView.as_view()),
    path("me/", TenantMeView.as_view()),
]
