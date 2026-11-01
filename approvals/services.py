"""One approval machine for every document type."""

from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import Approval

# Store-initiated documents pass Finance then Manager. Finance-initiated ones go
# straight to the Manager, and Manager-initiated documents are created approved.
CHAIN = {
    "STORE": ["FINANCE", "MANAGER"],
    "FINANCE": ["MANAGER"],
    "MANAGER": [],
}

STATUS_BY_STEP = {
    ("STORE", 0): "PENDING_FINANCE",
    ("STORE", 1): "PENDING_MANAGER",
    ("FINANCE", 0): "PENDING_MANAGER",
}


def initial_status(role):
    chain = CHAIN.get(role, ["MANAGER"])
    if not chain:
        return "APPROVED"
    return STATUS_BY_STEP.get((role, 0), "PENDING_MANAGER")


def step_for_status(status):
    return {"PENDING_FINANCE": "FINANCE", "PENDING_MANAGER": "MANAGER"}.get(status)


def decide(document, actor, decision, note=""):
    """Advance or terminate a document. Rejection is terminal by design."""
    if decision not in {"APPROVED", "REJECTED"}:
        raise ValidationError({"decision": "Use APPROVED or REJECTED."})

    required_role = step_for_status(document.status)
    if not required_role:
        raise ValidationError({"status": "This document is not awaiting a decision."})

    role = getattr(getattr(actor, "profile", None), "role", None)
    if role != required_role:
        raise PermissionDenied(f"This step belongs to {required_role}.")

    Approval.objects.create(
        tenant=document.tenant,
        entity_type=document.entity_type,
        entity_id=document.pk,
        step=required_role,
        decision=decision,
        actor=actor,
        role=role or "",
        note=note or "",
    )

    if decision == "REJECTED":
        document.status = "REJECTED"
        document.save(update_fields=["status"])
        return document

    if document.status == "PENDING_FINANCE":
        document.status = "PENDING_MANAGER"
        document.save(update_fields=["status"])
        return document

    document.apply_on_approval()
    document.status = "APPROVED"
    document.save(update_fields=["status"])
    return document
