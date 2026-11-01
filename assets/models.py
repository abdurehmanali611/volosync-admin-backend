from decimal import Decimal

from django.conf import settings
from django.db import models

from tenants.models import Tenant

MONEY = {"max_digits": 18, "decimal_places": 2}
QTY = {"max_digits": 14, "decimal_places": 3}


class Asset(models.Model):
    """Company property, assigned to clients. Manager registers, Finance assigns."""

    STATUS = (("AVAILABLE", "Available"), ("ASSIGNED", "Assigned"))
    CATEGORIES = (
        ("IT", "IT equipment"),
        ("FURNITURE", "Furniture"),
        ("VEHICLE", "Vehicle"),
        ("MACHINERY", "Machinery"),
        ("TOOL", "Tool"),
        ("OTHER", "Other"),
    )
    CONDITIONS = (
        ("NEW", "New"),
        ("GOOD", "Good"),
        ("FAIR", "Fair"),
        ("DAMAGED", "Damaged"),
    )

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="assets")
    asset_tag = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=20, choices=CATEGORIES, default="OTHER")
    serial_number = models.CharField(max_length=120, blank=True)
    quantity = models.DecimalField(**QTY, default=Decimal("1"))
    brand_model = models.CharField(max_length=120, blank=True)
    photo = models.ImageField(upload_to="assets/", null=True, blank=True)
    unit_cost = models.DecimalField(**MONEY, default=Decimal("0"))
    condition_at_registration = models.CharField(
        max_length=20, choices=CONDITIONS, default="GOOD"
    )
    useful_life_years = models.PositiveSmallIntegerField(default=5)
    status = models.CharField(max_length=20, choices=STATUS, default="AVAILABLE")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.asset_tag} · {self.name}"

    @property
    def book_value(self):
        years = max(1, self.useful_life_years)
        elapsed = (self.created_at.date() - self.created_at.date()).days / 365
        value = self.unit_cost - (self.unit_cost / years) * elapsed
        return max(Decimal("0"), value)

    @property
    def assignee_name(self):
        current = self.assignments.filter(is_active=True).first()
        return current.assignee_name if current else ""


class AssetAssignment(models.Model):
    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="assignments"
    )
    assignee_name = models.CharField(max_length=255)
    assigned_at = models.DateField()
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    is_active = models.BooleanField(default=True)
    removed_at = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-assigned_at"]

    def __str__(self):
        return f"{self.asset.asset_tag} → {self.assignee_name}"
