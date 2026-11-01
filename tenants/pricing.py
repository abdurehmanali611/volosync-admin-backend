"""Pricing helpers — the yearly fee recalculation (no quarterly concept)."""

from .models import PricingPlan


def default_plan():
    """The plan fees are recalculated from: default AND active."""
    return PricingPlan.objects.filter(is_default=True, active=True).first()


def suggest_annual_fee(tenant, plan=None):
    """yearly base + each switched-on module fee.

        annual_fee = yearly_base_fee
                     + inventory_yearly_fee   if tenant.module_inventory
                     + hr_yearly_fee          if tenant.module_hr
                     + sales_role_yearly_fee  if tenant.sales_role_enabled

    Returns None when there is no default active plan (nothing to
    recalculate from — the UI shows the current annual_fee unchanged).
    """
    if plan is None:
        plan = default_plan()
    if plan is None:
        return None
    total = plan.yearly_base_fee
    if tenant.module_inventory:
        total += plan.inventory_yearly_fee
    if tenant.module_hr:
        total += plan.hr_yearly_fee
    if tenant.sales_role_enabled:
        total += plan.sales_role_yearly_fee
    return total
