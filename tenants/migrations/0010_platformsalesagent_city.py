from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0009_alter_pricingplan_options_auditlog_reason_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="platformsalesagent",
            name="city",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
