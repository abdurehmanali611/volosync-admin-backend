from django.conf import settings
from django.db import models

from tenants.models import Tenant


class Approval(models.Model):
    DECISIONS = (("APPROVED", "Approved"), ("REJECTED", "Rejected"))

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="approvals")
    entity_type = models.CharField(max_length=40)
    entity_id = models.PositiveBigIntegerField()
    step = models.CharField(max_length=20)
    decision = models.CharField(max_length=20, choices=DECISIONS)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    role = models.CharField(max_length=20)
    note = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-decided_at"]

    def __str__(self):
        return f"{self.entity_type}#{self.entity_id} {self.step} {self.decision}"
