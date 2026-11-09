"""Apex review of owner-filed branch requests and multi-branch payments."""

from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

from user.platform import require_platform_admin

from .models import BranchPaymentRequest, BranchRequest
from .platform_views import log
from .services import (
    approve_branch_payment,
    approve_branch_request,
    branch_payment_row,
    branch_request_row,
    reject_branch_payment,
    reject_branch_request,
)


def _limit(request, default=200):
    try:
        return max(1, min(int(request.query_params.get("limit") or default), 500))
    except ValueError:
        return default


class BranchRequestListView(APIView):
    """Global branch-creation queue.

    GET   ?status=PENDING|APPROVED|REJECTED · ?parent=<id> · ?search=
    PATCH {id|<pk>, decision: APPROVED|REJECTED, note}
    """

    @require_platform_admin
    def get(self, request):
        rows = BranchRequest.objects.select_related(
            "parent", "created_by", "decided_by", "created_branch"
        )
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter in {"PENDING", "APPROVED", "REJECTED"}:
            rows = rows.filter(status=status_filter)
        parent = (request.query_params.get("parent") or "").strip()
        if parent.isdigit():
            rows = rows.filter(parent_id=int(parent))
        term = (request.query_params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(name__icontains=term)
                | Q(tin__icontains=term)
                | Q(parent__name__icontains=term)
                | Q(parent__tin__icontains=term)
            )
        total = rows.count()
        payload = [branch_request_row(row) for row in rows[: _limit(request)]]
        return Response({"data": payload, "meta": {"total": total}})

    @require_platform_admin
    def patch(self, request, pk=None):
        row_id = pk or request.data.get("id")
        row = (
            BranchRequest.objects.select_related("parent", "created_branch")
            .filter(pk=row_id)
            .first()
        )
        if row is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if row.status != "PENDING":
            return Response(
                {"errors": {"message": "This request was already decided."}},
                status=400,
            )
        decision = (
            request.data.get("decision") or request.data.get("status") or ""
        ).upper()
        note = (
            request.data.get("note")
            or request.data.get("decision_note")
            or request.data.get("reason")
            or ""
        ).strip()

        if decision == "REJECTED":
            if len(note) < 4:
                return Response(
                    {
                        "errors": {
                            "message": "Explain the rejection — the owner sees this note."
                        }
                    },
                    status=400,
                )
            reject_branch_request(row, request.user, note)
            log(
                request.user,
                "BRANCH_REQUEST_REJECTED",
                "branch_request",
                row.id,
                f"{row.parent.name} · {row.name}",
                reason=note,
            )
            return Response({"data": branch_request_row(row)})

        if decision != "APPROVED":
            return Response(
                {"errors": {"message": "decision must be APPROVED or REJECTED."}},
                status=400,
            )
        try:
            branch = approve_branch_request(row, request.user, note)
        except ValueError as error:
            return Response({"errors": {"message": str(error)}}, status=400)
        log(
            request.user,
            "BRANCH_REQUEST_APPROVED",
            "branch_request",
            row.id,
            f"{row.parent.name} · {branch.name}",
            reason=note,
        )
        return Response(
            {
                "data": branch_request_row(row),
                "branch": {"id": branch.id, "tin": branch.tin, "name": branch.name},
            }
        )


class BranchPaymentRequestListView(APIView):
    """Queue of owner multi-branch yearly payments.

    GET   ?status= · ?parent=<id> · ?search=
    PATCH {id|<pk>, decision: APPROVED|PARTIALLY_APPROVED|REJECTED,
           excluded_line_ids?|included_line_ids?, note}
    """

    @require_platform_admin
    def get(self, request):
        rows = BranchPaymentRequest.objects.select_related(
            "parent", "submitted_by", "decided_by"
        ).prefetch_related("lines__tenant")
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter in {
            "PENDING",
            "APPROVED",
            "PARTIALLY_APPROVED",
            "REJECTED",
        }:
            rows = rows.filter(status=status_filter)
        parent = (request.query_params.get("parent") or "").strip()
        if parent.isdigit():
            rows = rows.filter(parent_id=int(parent))
        term = (request.query_params.get("search") or "").strip()
        if term:
            rows = rows.filter(
                Q(transaction_ref__icontains=term)
                | Q(parent__name__icontains=term)
                | Q(parent__tin__icontains=term)
            )
        total = rows.count()
        payload = [branch_payment_row(row) for row in rows[: _limit(request)]]
        return Response({"data": payload, "meta": {"total": total}})

    @require_platform_admin
    def patch(self, request, pk=None):
        row_id = pk or request.data.get("id")
        row = (
            BranchPaymentRequest.objects.select_related("parent")
            .prefetch_related("lines__tenant")
            .filter(pk=row_id)
            .first()
        )
        if row is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if row.status != "PENDING":
            return Response(
                {"errors": {"message": "This payment was already decided."}},
                status=400,
            )
        decision = (
            request.data.get("decision") or request.data.get("status") or ""
        ).upper()
        note = (
            request.data.get("note")
            or request.data.get("decision_note")
            or request.data.get("reason")
            or ""
        ).strip()

        if decision == "REJECTED":
            if len(note) < 4:
                return Response(
                    {
                        "errors": {
                            "message": "Explain the rejection — the owner sees this note."
                        }
                    },
                    status=400,
                )
            reject_branch_payment(row, request.user, note)
            log(
                request.user,
                "BRANCH_PAYMENT_REJECTED",
                "branch_payment_request",
                row.id,
                row.transaction_ref,
                reason=note,
            )
            return Response({"data": branch_payment_row(row)})

        if decision not in {"APPROVED", "PARTIALLY_APPROVED"}:
            return Response(
                {
                    "errors": {
                        "message": "decision must be APPROVED, PARTIALLY_APPROVED or REJECTED."
                    }
                },
                status=400,
            )

        all_ids = {line.id for line in row.lines.all()}
        excluded = request.data.get("excluded_line_ids")
        included = request.data.get("included_line_ids")
        if included is not None:
            keep = {int(value) for value in included}
        elif excluded is not None:
            keep = all_ids - {int(value) for value in excluded}
        else:
            keep = all_ids
        try:
            approve_branch_payment(row, request.user, keep, note)
        except (ValueError, TypeError) as error:
            return Response({"errors": {"message": str(error)}}, status=400)
        log(
            request.user,
            "BRANCH_PAYMENT_APPROVED",
            "branch_payment_request",
            row.id,
            f"{row.transaction_ref} · {len(keep)}/{len(all_ids)} branches",
            reason=note,
        )
        return Response({"data": branch_payment_row(row)})
