from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from tenants.models import Tenant

QTY = {"max_digits": 14, "decimal_places": 3}
MONEY = {"max_digits": 18, "decimal_places": 2}

STATUS_CHOICES = (
    ("DRAFT", "Draft"),
    ("PENDING_FINANCE", "Pending finance"),
    ("PENDING_MANAGER", "Pending manager"),
    ("APPROVED", "Approved"),
    ("REJECTED", "Rejected"),
)


def next_voucher(model, tenant, prefix):
    year = timezone.now().year
    count = model.objects.filter(
        tenant=tenant, voucher_number__startswith=f"{prefix}-{year}"
    ).count()
    return f"{prefix}-{year}-{count + 1:06d}"


class Item(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=40, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("tenant", "name", "category")]

    def __str__(self):
        return self.name


class StockLot(models.Model):
    """One registered line = one voucher. Inventory is voucher-lot based."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="lots")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="lots")
    voucher_number = models.CharField(max_length=40, unique=True)
    quantity = models.DecimalField(**QTY)
    remaining_quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY)
    unit_sales = models.DecimalField(**MONEY)
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("15.00")
    )
    registration_date = models.DateField()
    expiry_date = models.DateField(null=True, blank=True)
    supplier_name = models.CharField(max_length=255, blank=True)
    supplier_phone = models.CharField(max_length=40, blank=True)
    supplier_address = models.CharField(max_length=255, blank=True)
    supplier_tin = models.CharField(max_length=50, blank=True)
    note = models.TextField(blank=True)
    image = models.ImageField(upload_to="lots/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.voucher_number} · {self.item.name}"

    @property
    def price_with_vat(self):
        return (self.unit_sales * (1 + self.vat_rate / 100)).quantize(
            Decimal("0.01")
        )

    @property
    def paid_amount(self):
        base = self.quantity * self.unit_cost
        return (base + base * self.vat_rate / 100).quantize(Decimal("0.01"))

    @property
    def stocked_out_quantity(self):
        return self.quantity - self.remaining_quantity


class StockMovement(models.Model):
    TYPES = (
        ("REGISTRATION", "Registration"),
        ("STOCKOUT", "Stock out"),
        ("SALE", "Sale"),
        ("SALE_RETURN", "Sale return"),
        ("PROMOTION", "Promotion"),
        ("PRICE_CHANGE", "Price change"),
    )

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="movements"
    )
    lot = models.ForeignKey(StockLot, on_delete=models.CASCADE, related_name="movements")
    type = models.CharField(max_length=20, choices=TYPES)
    quantity_delta = models.DecimalField(**QTY)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    role = models.CharField(max_length=20, blank=True)
    ref_type = models.CharField(max_length=40, blank=True)
    ref_id = models.PositiveBigIntegerField(null=True, blank=True)
    note = models.TextField(blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-at"]


class RegistrationRequest(models.Model):
    entity_type = "REGISTRATION"

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="registrations"
    )
    voucher_number = models.CharField(max_length=40, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="registrations",
    )
    supplier_name = models.CharField(max_length=255, blank=True)
    supplier_phone = models.CharField(max_length=40, blank=True)
    supplier_address = models.CharField(max_length=255, blank=True)
    supplier_tin = models.CharField(max_length=50, blank=True)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.voucher_number

    @property
    def total_value(self):
        return sum((line.total_cost for line in self.lines.all()), Decimal("0"))

    def apply_on_approval(self):
        """Create the voucher lots and their opening movements."""
        with transaction.atomic():
            for line in self.lines.all():
                if line.lot_id:
                    continue
                item, _ = Item.objects.get_or_create(
                    tenant=self.tenant,
                    name=line.item_name,
                    category=line.category,
                    defaults={"unit": line.unit},
                )
                lot = StockLot.objects.create(
                    tenant=self.tenant,
                    item=item,
                    voucher_number=f"{self.voucher_number}-{line.pk:03d}",
                    quantity=line.quantity,
                    remaining_quantity=line.quantity,
                    unit_cost=line.unit_cost,
                    unit_sales=line.unit_sales,
                    vat_rate=line.vat_rate,
                    registration_date=line.registration_date,
                    expiry_date=line.expiry_date,
                    supplier_name=self.supplier_name,
                    supplier_phone=self.supplier_phone,
                    supplier_address=self.supplier_address,
                    supplier_tin=self.supplier_tin,
                    note=line.note,
                )
                line.lot = lot
                line.save(update_fields=["lot"])
                StockMovement.objects.create(
                    tenant=self.tenant,
                    lot=lot,
                    type="REGISTRATION",
                    quantity_delta=line.quantity,
                    actor=self.created_by,
                    role="STORE",
                    ref_type=self.entity_type,
                    ref_id=self.pk,
                    note=self.voucher_number,
                )


class RegistrationLine(models.Model):
    request = models.ForeignKey(
        RegistrationRequest, on_delete=models.CASCADE, related_name="lines"
    )
    lot = models.ForeignKey(StockLot, null=True, blank=True, on_delete=models.SET_NULL)
    item_name = models.CharField(max_length=255)
    category = models.CharField(max_length=120, blank=True)
    unit = models.CharField(max_length=40, blank=True)
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY)
    unit_sales = models.DecimalField(**MONEY)
    vat_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("15.00")
    )
    registration_date = models.DateField()
    expiry_date = models.DateField(null=True, blank=True)
    note = models.TextField(blank=True)

    @property
    def price_with_vat(self):
        return (self.unit_sales * (1 + self.vat_rate / 100)).quantize(
            Decimal("0.01")
        )

    @property
    def paid_amount(self):
        return (self.quantity * self.unit_cost * (1 + self.vat_rate / 100)).quantize(
            Decimal("0.01")
        )

    @property
    def total_cost(self):
        return self.paid_amount


class StockOutRequest(models.Model):
    entity_type = "STOCKOUT"
    MODES = (("SINGLE", "Single"), ("BATCH", "Batch"))

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="stockouts"
    )
    voucher_number = models.CharField(max_length=40, unique=True)
    mode = models.CharField(max_length=10, choices=MODES, default="SINGLE")
    date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="stockouts",
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.voucher_number

    def apply_on_approval(self):
        with transaction.atomic():
            for assignment in self.assignments.select_related("lot"):
                lot = StockLot.objects.select_for_update().get(pk=assignment.lot_id)
                if assignment.quantity > lot.remaining_quantity:
                    raise ValidationError(
                        {
                            "quantity": (
                                f"{lot.voucher_number} only has "
                                f"{lot.remaining_quantity} available."
                            )
                        }
                    )
                lot.remaining_quantity -= assignment.quantity
                lot.save(update_fields=["remaining_quantity"])
                StockMovement.objects.create(
                    tenant=self.tenant,
                    lot=lot,
                    type="STOCKOUT",
                    quantity_delta=-assignment.quantity,
                    actor=self.created_by,
                    role="STORE",
                    ref_type=self.entity_type,
                    ref_id=self.pk,
                    note=assignment.change_reason,
                )


class StockAssignment(models.Model):
    request = models.ForeignKey(
        StockOutRequest, on_delete=models.CASCADE, related_name="assignments"
    )
    lot = models.ForeignKey(
        StockLot, on_delete=models.PROTECT, related_name="assignments"
    )
    sales_agent = models.ForeignKey(
        "sales.SalesAgent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assignments",
    )
    quantity = models.DecimalField(**QTY)
    changing_amount = models.DecimalField(**MONEY, default=Decimal("0"))
    change_reason = models.CharField(max_length=255, blank=True)
    unit_sales_at_assignment = models.DecimalField(**MONEY)
