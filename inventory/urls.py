from django.urls import path

from .views import (
    AgentOptionsView,
    DashboardView,
    InventorySummaryView,
    LotDetailView,
    LotListView,
    MovementListView,
    RegistrationDecisionView,
    RegistrationListCreateView,
    StockOutDecisionView,
    StockOutListCreateView,
)

urlpatterns = [
    path("dashboard/", DashboardView.as_view()),
    path("lots/", LotListView.as_view()),
    path("lots/<int:pk>/", LotDetailView.as_view()),
    path("summary/", InventorySummaryView.as_view()),
    path("movements/", MovementListView.as_view()),
    path("agents/", AgentOptionsView.as_view()),
    path("registrations/", RegistrationListCreateView.as_view()),
    path("registrations/<int:pk>/decision/", RegistrationDecisionView.as_view()),
    path("stockouts/", StockOutListCreateView.as_view()),
    path("stockouts/<int:pk>/decision/", StockOutDecisionView.as_view()),
]
