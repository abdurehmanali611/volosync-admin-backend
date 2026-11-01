from django.conf import settings
from django.db import models

from tenants.models import Tenant


class Profile(models.Model):
    ROLE_CHOICES = (
        ("STORE", "Store"),
        ("FINANCE", "Finance"),
        ("MANAGER", "Manager"),
        ("HR", "HR"),
        ("SALES", "Sales"),
        ("OWNER", "Owner"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="staff"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="STORE")
    phone = models.CharField(max_length=40, blank=True)
    must_change_password = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"
