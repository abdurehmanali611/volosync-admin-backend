"""Backfill for the payment queue + the default pricing plan.

- Every SubscriptionPayment created BEFORE the queue existed (submitted_at
  is NULL) is flipped to APPROVED — they were recorded as already-paid and
  already applied to their tenants. New submissions always set submitted_at,
  so they are never touched.
- Seeds ONE default PricingPlan (yearly fees, no quarterly concept) if the
  catalog is empty. This is the only data this migration writes — it never
  modifies founder data, and it is a no-op when a plan already exists.
"""

from django.db import migrations


def backfill_and_seed(apps, schema_editor):
    SubscriptionPayment = apps.get_model("tenants", "SubscriptionPayment")
    SubscriptionPayment.objects.filter(submitted_at__isnull=True).update(status="APPROVED")

    PricingPlan = apps.get_model("tenants", "PricingPlan")
    if not PricingPlan.objects.exists():
        PricingPlan.objects.create(
            name="VoloSync Standard",
            setup_fee="30000.00",
            yearly_base_fee="20000.00",
            inventory_yearly_fee="24000.00",
            hr_yearly_fee="16000.00",
            sales_role_yearly_fee="8000.00",
            is_default=True,
            active=True,
        )


def unseed(apps, schema_editor):
    """Nothing to reverse: the backfill only reflects pre-existing reality,
    and removing the seeded plan could orphan a tenant's fee expectations."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0006_platformsalesagent_pricingplan_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_and_seed, unseed),
    ]
