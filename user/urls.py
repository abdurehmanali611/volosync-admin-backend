from django.urls import path

from .views import ChangePasswordView, MeView

urlpatterns = [
    path("me/", MeView.as_view()),
    path("change_password/", ChangePasswordView.as_view()),
]
