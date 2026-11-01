# Mirrors the tenants.0002 migration already applied by the volosync repo.
# The database is shared, so the columns exist; this file only keeps the
# admin repo's migration graph consistent.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="subscription_paid_on",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tenant",
            name="subscription_transaction_id",
            field=models.CharField(blank=True, max_length=60),
        ),
    ]
