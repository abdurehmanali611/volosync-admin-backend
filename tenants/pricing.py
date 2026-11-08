"""Pricing helpers — the yearly fee recalculation (no quarterly concept)."""

from .models import PricingPlan

#: The console builds a plan's name from the modules it covers
#: (`lib/apex/actions.ts` → `planNameFromModules`), so the name is the module
#: set that plan prices.
PLAN_NAME_MODULES = {
    "Base package": (),
    "Standard plan": ("Inventory", "HR Module"),
    "Basic plan": ("Inventory", "Sales Role"),
    "Complete plan": ("Inventory", "HR Module", "Sales Role"),
}

MODULE_NAMES = ("Inventory", "HR Module", "Sales Role")


def default_plan():
    """The plan fees are recalculated from: default AND active.

    Falls back to the first active plan so a catalog imported without the
    default flag still prices tenants instead of failing with
    "No default active pricing plan to price from."
    """
    return (
        PricingPlan.objects.filter(is_default=True, active=True).first()
        or PricingPlan.objects.filter(active=True).first()
    )


def plan_modules(plan):
    """The module set encoded in a plan name."""
    name = (plan.name or "").strip()
    if name in PLAN_NAME_MODULES:
        return PLAN_NAME_MODULES[name]
    return tuple(
        part.strip() for part in name.split("+") if part.strip() in MODULE_NAMES
    )


def tenant_modules(tenant):
    """The module switches that decide which plan prices this tenant."""
    modules = []
    if tenant.module_inventory:
        modules.append("Inventory")
    if tenant.module_hr:
        modules.append("HR Module")
    if tenant.sales_role_enabled:
        modules.append("Sales Role")
    return tuple(modules)


def catalog_plan(tenant):
    """The saved plan that prices this tenant's module mix.

    Returns ``(plan, exact)``. An exact module-set match uses that plan's
    saved yearly fee as-is; otherwise the default (or first active) plan is
    used and its per-module add-ons are added.
    """
    wanted = set(tenant_modules(tenant))
    for plan in PricingPlan.objects.filter(active=True):
        if set(plan_modules(plan)) == wanted:
            return plan, True
    return default_plan(), False


def suggest_annual_fee(tenant, plan=None):
    """yearly base + each switched-on module fee.

        annual_fee = yearly_base_fee
                     + inventory_yearly_fee   if tenant.module_inventory
                     + hr_yearly_fee          if tenant.module_hr
                     + sales_role_yearly_fee  if tenant.sales_role_enabled

    Without an explicit plan, the catalog plan that matches the tenant's
    modules wins, so the console shows the fee the tenant actually saves.
    """
    if plan is None:
        plan, exact = catalog_plan(tenant)
        if plan is None:
            return None
        if exact:
            return plan.yearly_base_fee
    total = plan.yearly_base_fee
    if tenant.module_inventory:
        total += plan.inventory_yearly_fee
    if tenant.module_hr:
        total += plan.hr_yearly_fee
    if tenant.sales_role_enabled:
        total += plan.sales_role_yearly_fee
    return total


def suggest_fees(tenant):
    """``(plan, yearly_fee, setup_fee)`` for this tenant's saved module mix.

    Both values come from the same plan, so the console's two catalog tiles
    and "Apply catalog pricing" can never disagree.
    """
    plan, exact = catalog_plan(tenant)
    if plan is None:
        return None, None, None
    if exact:
        yearly = plan.yearly_base_fee
    else:
        yearly = suggest_annual_fee(tenant, plan)
    return plan, yearly, plan.setup_fee
