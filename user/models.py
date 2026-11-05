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
    # Apex can switch a single staff login off without touching the tenant's
    # other accounts — the reason and actor show up in the tenant's Staff tab
    # and in the audit log.
    login_disabled = models.BooleanField(default=False)
    login_disabled_reason = models.CharField(max_length=255, blank=True)
    login_disabled_at = models.DateTimeField(null=True, blank=True)
    login_disabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="staff_logins_disabled",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"
