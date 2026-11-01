from decimal import Decimal

from django.conf import settings
from django.db import models


class Tenant(models.Model):
    MODULE_CHOICES = (("INVENTORY", "Inventory"), ("HR", "HR"))
    # Credentials is always-on (basic), never a billable/requestable module.
    REQUIRED_MODULES = ("CREDENTIALS",)

    name = models.CharField(max_length=255)
    tin = models.CharField(max_length=50, unique=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.CharField(max_length=255, blank=True)
    logo_url = models.URLField(blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    module_inventory = models.BooleanField(default=True)
    module_hr = models.BooleanField(default=True)
    sales_role_enabled = models.BooleanField(default=False)
    sales_agent = models.ForeignKey(
        "tenants.PlatformSalesAgent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tenants",
    )
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="branches"
    )
    is_main = models.BooleanField(default=False)
    subscription_expires_on = models.DateField(null=True, blank=True)
    subscription_transaction_id = models.CharField(max_length=60, blank=True)
    subscription_paid_on = models.DateField(null=True, blank=True)
    subscription_note = models.CharField(max_length=255, blank=True)
    annual_fee = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0.00")
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_branch(self):
        return self.parent_id is not None

    @property
    def subscription_state(self):
        from django.utils import timezone

        if not self.subscription_expires_on:
            return "UNPAID"
        days = (self.subscription_expires_on - timezone.localdate()).days
        if days < 0:
            return "EXPIRED"
        if days <= 30:
            return "DUE_SOON"
        return "ACTIVE"


class SubscriptionPayment(models.Model):
    """Yearly subscription ledger — one row per transaction the owner reports.

    Yearly only (spec §9: no quarterly billing anywhere). The owner submits
    {channel, transaction_ref, amount, years} → row lands PENDING; Apex
    approves → the tenant is extended 365×years days; a rejection just
    records the decision. Rows recorded before the queue existed carry
    status APPROVED (data migration).
    """

    CHANNEL_CHOICES = (
        ("TELEBIRR", "Telebirr"),
        ("BANK", "Bank"),
        ("CASH", "Cash"),
        ("OTHER", "Other"),
    )
    STATUS = (("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="subscription_payments"
    )
    transaction_id = models.CharField(max_length=60)
    channel = models.CharField(
        max_length=20, choices=CHANNEL_CHOICES, blank=True, default="OTHER"
    )
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    years = models.PositiveSmallIntegerField(default=1)
    paid_on = models.DateField()
    covers_until = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subscription_submissions",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="subscription_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]

    def __str__(self):
        return f"{self.tenant.name} · {self.transaction_id}"
from django.conf import settings
from django.db import models


class Feedback(models.Model):
    """Message a wholesaler sends to Apex from inside VoloSync."""

    STATUS = (("OPEN", "Open"), ("READ", "Read"), ("RESOLVED", "Resolved"))

    tenant = models.ForeignKey(
        "tenants.Tenant", null=True, on_delete=models.SET_NULL, related_name="feedback"
    )
    subject = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    category = models.CharField(max_length=40, blank=True, default="GENERAL")
    status = models.CharField(max_length=20, choices=STATUS, default="OPEN")
    contact = models.CharField(max_length=120, blank=True)
    unread = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.subject} · {self.tenant}"


class AuditLog(models.Model):
    """Everything an operator does in the platform console."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=80)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    detail = models.CharField(max_length=255, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.action} · {self.target_type}{self.target_id}"


class SetupPayment(models.Model):
    """One-off onboarding fee a wholesaler pays before going live."""

    STATUS = (("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="setup_payments"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    transaction_ref = models.CharField(max_length=60, blank=True)
    channel = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    note = models.CharField(max_length=255, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="setup_payment_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant.name} · setup {self.amount}"


class ModuleRequest(models.Model):
    """A tenant asking Apex to switch a module on or off."""

    STATUS = (("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="module_requests"
    )
    requested_modules = models.CharField(max_length=120)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.tenant.name} · {self.requested_modules}"


class PricingPlan(models.Model):
    """Catalog of platform fees. Yearly only — spec §9 forbids quarterly.

    Exactly one plan is `is_default` (enforced in the API): the default
    active plan drives the tenant fee recalculation
    (`base + inventory + hr + sales_role`, each added only when the module
    is on for that tenant).
    """

    name = models.CharField(max_length=100, unique=True)
    setup_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    yearly_base_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    inventory_yearly_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    hr_yearly_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    sales_role_yearly_fee = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    is_default = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self):
        return self.name


class PlatformSalesAgent(models.Model):
    """Apex's own seller — NOT a tenant's sales agent (those live in `sales`)."""

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    sex = models.CharField(max_length=10, blank=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
