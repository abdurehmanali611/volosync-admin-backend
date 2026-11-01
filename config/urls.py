from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from tenants.payment_views import (
    SetupPaymentSubmitView,
    SubscriptionDecisionView,
    SubscriptionSubmitView,
)
from tenants.platform_views import ModulesCatalogView, PlatformReportsView
from tenants.platform_views import PricingPlanDetailView, PricingPlanListCreateView
from tenants.sales_agents import PlatformSalesAgentDetailView, PlatformSalesAgentListCreateView
from user.views import CustomTokenObtainPairView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/login/", CustomTokenObtainPairView.as_view(), name="login"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/user/", include("user.urls")),
    path("api/tenants/", include("tenants.urls")),
    # Payment verification queue (owner submits, Apex decides)
    path("api/payments/subscription/", SubscriptionSubmitView.as_view()),
    path("api/payments/subscription/<int:pk>/decision/", SubscriptionDecisionView.as_view()),
    path("api/payments/setup/submit/", SetupPaymentSubmitView.as_view()),
    # Pricing catalog + fee recalculation source
    path("api/pricing/", PricingPlanListCreateView.as_view()),
    path("api/pricing/<int:pk>/", PricingPlanDetailView.as_view()),
    # Modules catalog + platform reports (client-side export)
    path("api/modules/catalog/", ModulesCatalogView.as_view()),
    path("api/reports/platform/", PlatformReportsView.as_view()),
    # Apex's own sales agents (not tenant agents)
    path("api/sales-agents/", PlatformSalesAgentListCreateView.as_view()),
    path("api/sales-agents/<int:pk>/", PlatformSalesAgentDetailView.as_view()),
    path("api/inventory/", include("inventory.urls")),
    path("api/sales/", include("sales.urls")),
    path("api/assets/", include("assets.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
