"""Wipe demo/seeded data so the platform starts clean and ready to use.

Keeps:
  * Django superusers (the Apex console logins)
  * the default pricing plan (fee math depends on it)

Deletes everything else — tenants, their staff, inventory, sales agents,
assets, payment queues, module requests, feedback, audit history and the
test sales-agent rows. Run with ``--dry-run`` to preview the counts.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from assets.models import Asset
from inventory.models import (
    Item,
    RegistrationLine,
    RegistrationRequest,
    StockAssignment,
    StockLot,
    StockMovement,
    StockOutRequest,
)
from sales.models import SalesAgent, SalesSettlement
from tenants.models import (
    AuditLog,
    Feedback,
    ModuleRequest,
    PlatformSalesAgent,
    SetupPayment,
    SubscriptionPayment,
    Tenant,
)
from user.models import Profile


# Tables owned by the sibling volosync-emp / volosync-ats backends that share
# this database and hold a hard FK to tenants_tenant. They are demo rows for
# the seeded wholesaler, so they are cleared before the tenant is deleted.
SIBLING_APP_TABLES = (
    "hr_attendance",
    "hr_attendancecorrection",
    "hr_checklistrun",
    "hr_checklisttemplate",
    "hr_department",
    "hr_employee",
    "hr_hrdocument",
    "hr_hrworkflow",
    "hr_incident",
    "hr_incidenttype",
    "hr_leaverequest",
    "hr_leavetype",
    "hr_payrollentry",
    "hr_peopleevent",
    "hr_shifttemplate",
)


class Command(BaseCommand):
    help = "Remove seeded/demo data, keeping superusers and the default pricing plan."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only report what would go.")

    def handle(self, *args, **options):
        dry = options["dry_run"]

        # Tenants first, cascading to their stock, sales, assets and queues.
        tenants = Tenant.objects.all()
        demo_users = User.objects.filter(is_superuser=False)

        counts = {
            "tenants": tenants.count(),
            "users (non-superuser)": demo_users.count(),
            "profiles": Profile.objects.count(),
            "stock lots": StockLot.objects.count(),
            "stock movements": StockMovement.objects.count(),
            "items": Item.objects.count(),
            "registration requests": RegistrationRequest.objects.count(),
            "registration lines": RegistrationLine.objects.count(),
            "stock out requests": StockOutRequest.objects.count(),
            "sales assignments": StockAssignment.objects.count(),
            "sales settlements": SalesSettlement.objects.count(),
            "tenant sales agents": SalesAgent.objects.count(),
            "assets": Asset.objects.count(),
            "subscription payments": SubscriptionPayment.objects.count(),
            "setup payments": SetupPayment.objects.count(),
            "module requests": ModuleRequest.objects.count(),
            "feedback": Feedback.objects.count(),
            "audit log": AuditLog.objects.count(),
            "platform sales agents": PlatformSalesAgent.objects.count(),
        }
        with connection.cursor() as cursor:
            for table in SIBLING_APP_TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                counts[f"{table} (HR app)"] = cursor.fetchone()[0]

        for label, count in counts.items():
            self.stdout.write(f"  {label}: {count}")

        if dry:
            self.stdout.write(self.style.WARNING("Dry run — nothing deleted."))
            return

        with transaction.atomic():
            # The HR app belongs to volosync-emp and is not installed here, so
            # its rows must be cleared by table name before the tenant can go.
            if Tenant.objects.exists():
                with connection.cursor() as cursor:
                    cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                    try:
                        for table in SIBLING_APP_TABLES:
                            cursor.execute(f"DELETE FROM `{table}`")
                    finally:
                        cursor.execute("SET FOREIGN_KEY_CHECKS=1")

            # Children before tenants so FK cascades stay predictable.
            # Deletes go through querysets (never a blanket DELETE) so MySQL
            # safe-update mode and table names of the sibling HR/ATS repos
            # are never touched from this backend.
            SalesSettlement.objects.all().delete()
            StockAssignment.objects.all().delete()
            StockOutRequest.objects.all().delete()
            StockMovement.objects.all().delete()
            RegistrationLine.objects.all().delete()
            RegistrationRequest.objects.all().delete()
            SalesAgent.objects.all().delete()
            StockLot.objects.all().delete()
            Item.objects.all().delete()
            Asset.objects.all().delete()

            SubscriptionPayment.objects.all().delete()
            SetupPayment.objects.all().delete()
            ModuleRequest.objects.all().delete()
            Feedback.objects.all().delete()
            AuditLog.objects.all().delete()
            PlatformSalesAgent.objects.all().delete()

            # Tenants cascade to their remaining children (stock, sales).
            Tenant.objects.all().delete()

            # Demo logins (store/finance/manager/hr/owner/agent-*) are removed;
            # the console superusers stay.
            Profile.objects.all().delete()
            demo_users.delete()

        remaining = {
            "superusers": User.objects.filter(is_superuser=True).count(),
            "tenants": Tenant.objects.count(),
            "users": User.objects.count(),
        }
        self.stdout.write(
            self.style.SUCCESS(
                "Demo data cleared — "
                + ", ".join(f"{k}: {v}" for k, v in remaining.items())
                + ". Default pricing plan kept."
            )
        )
