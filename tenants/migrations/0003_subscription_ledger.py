from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Yearly subscription ledger.

    The shared database already carries tenants.0002 (the subscription fields
    added by the volosync repo), so this migration only creates the ledger.
    """

    dependencies = [
        ("tenants", "0002_tenant_subscription_paid_on_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SubscriptionPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("transaction_id", models.CharField(max_length=60)),
                ("amount", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=14)),
                ("years", models.PositiveSmallIntegerField(default=1)),
                ("paid_on", models.DateField()),
                ("covers_until", models.DateField()),
                ("note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("recorded_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subscription_payments", to="tenants.tenant")),
            ],
            options={"ordering": ["-paid_on", "-id"]},
        ),
    ]
