from django.urls import path

from .views import (
    SalesAgentListCreateView,
    SalesAgentLoginView,
    SalesAgentRankingView,
    SettlementDecisionView,
    SettlementListCreateView,
)

urlpatterns = [
    path("agents/", SalesAgentListCreateView.as_view()),
    path("agents/login/", SalesAgentLoginView.as_view()),
    path("agents/ranking/", SalesAgentRankingView.as_view()),
    path("settlements/", SettlementListCreateView.as_view()),
    path("settlements/<int:pk>/decision/", SettlementDecisionView.as_view()),
]
