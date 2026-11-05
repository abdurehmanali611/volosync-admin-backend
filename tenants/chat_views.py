"""Apex ↔ wholesaler chat (the console's Messages/Chat section).

One thread per tenant: the console opens it, replies as APEX and can close or
re-open it. Wholesaler-side rows are written by the VoloSync app against the
same tables, so this file never needs to know how the tenant authenticated.
"""

from django.db.models import Count, Max, Q
from rest_framework.response import Response
from rest_framework.views import APIView

from user.platform import require_platform_admin

from .models import ChatMessage, ChatThread, Tenant
from .platform_views import log

# A broadcast fans out to at most this many tenants in one call.
BROADCAST_LIMIT = 50


def message_row(message):
    return {
        "id": message.id,
        "thread": message.thread_id,
        "side": message.side,
        "body": message.body,
        "image_url": message.image_url,
        "actor": message.actor.username if message.actor else "",
        "read_by_apex": message.read_by_apex,
        "read_by_tenant": message.read_by_tenant,
        "created_at": message.created_at,
    }


def thread_row(thread, last_message=None, unread_for_apex=0, total=0):
    return {
        "id": thread.id,
        "tenant": thread.tenant_id,
        "tenant_name": thread.tenant.name,
        "tin": thread.tenant.tin,
        "subject": thread.subject,
        "status": thread.status,
        "last_message": message_row(last_message) if last_message else None,
        "unread_for_apex": unread_for_apex,
        "messages": total,
        "opened_by": thread.opened_by.username if thread.opened_by else "",
        "closed_by": thread.closed_by.username if thread.closed_by else "",
        "closed_at": thread.closed_at,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
    }


def open_thread(tenant, subject, actor):
    """The tenant's thread, re-opened and titled when it was closed/blank."""
    thread, _ = ChatThread.objects.get_or_create(
        tenant=tenant, defaults={"subject": subject, "opened_by": actor}
    )
    changed = []
    if thread.status != "OPEN":
        thread.status = "OPEN"
        thread.closed_by = None
        thread.closed_at = None
        changed += ["status", "closed_by", "closed_at"]
    if subject and thread.subject != subject:
        thread.subject = subject
        changed.append("subject")
    if not thread.opened_by_id:
        thread.opened_by = actor
        changed.append("opened_by")
    if changed:
        thread.save(update_fields=changed + ["updated_at"])
    return thread


def _search(queryset, term):
    if not term:
        return queryset
    return queryset.filter(
        Q(tenant__name__icontains=term)
        | Q(tenant__tin__icontains=term)
        | Q(subject__icontains=term)
        | Q(messages__body__icontains=term)
    ).distinct()


def _last_messages(thread_ids):
    """{thread_id: newest message} for a page of threads, in two queries."""
    if not thread_ids:
        return {}
    newest = ChatMessage.objects.filter(thread_id__in=thread_ids).values("thread").annotate(
        last=Max("id")
    )
    return {
        message.thread_id: message
        for message in ChatMessage.objects.filter(
            id__in=[row["last"] for row in newest]
        ).select_related("actor")
    }


class ChatThreadDirectoryView(APIView):
    """GET the thread list, POST to open one and send the first message."""

    @require_platform_admin
    def get(self, request):
        threads = (
            ChatThread.objects.select_related("tenant", "opened_by", "closed_by")
            .annotate(
                total_messages=Count("messages", distinct=True),
                unread=Count(
                    "messages",
                    filter=Q(messages__side="TENANT", messages__read_by_apex=False),
                    distinct=True,
                ),
            )
        )
        threads = _search(threads, (request.query_params.get("search") or "").strip())
        status_filter = (request.query_params.get("status") or "").upper()
        if status_filter in ("OPEN", "CLOSED"):
            threads = threads.filter(status=status_filter)
        if (request.query_params.get("unread") or "").lower() in ("1", "true", "yes"):
            threads = threads.filter(unread__gt=0)

        total = threads.count()
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 200), 500))
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError:
            limit, offset = 200, 0
        page = list(threads[offset : offset + limit])
        last_by_thread = _last_messages([thread.id for thread in page])
        rows = [
            thread_row(
                thread,
                last_by_thread.get(thread.id),
                thread.unread,
                thread.total_messages,
            )
            for thread in page
        ]
        return Response({"data": rows, "meta": {"total": total, "limit": limit, "offset": offset}})

    @require_platform_admin
    def post(self, request):
        tenant = Tenant.objects.filter(pk=request.data.get("tenant")).first()
        if tenant is None:
            return Response({"errors": {"message": "Pick a wholesaler."}}, status=400)
        body = (request.data.get("body") or "").strip()
        image_url = (request.data.get("image_url") or "").strip()
        if not body and not image_url:
            return Response(
                {"errors": {"message": "Write a message before sending it."}}, status=400
            )
        subject = (request.data.get("subject") or "").strip()
        thread = open_thread(tenant, subject, request.user)
        message = ChatMessage.objects.create(
            thread=thread, side="APEX", body=body, image_url=image_url, actor=request.user,
            read_by_apex=True,
        )
        log(request.user, "CHAT_STARTED", "chat_thread", thread.pk, tenant.name)
        return Response(
            {"data": {"thread": thread_row(thread, message, 0, 1)}}, status=201
        )


class ChatThreadView(APIView):
    """One tenant's conversation, addressed by TENANT id (stable console link).

    GET   → thread + every message, oldest first.
    POST  → {body, image_url?} append an APEX message.
    PATCH → {action: close|reopen|read} — `read` marks wholesaler messages read.
    """

    def _thread(self, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return None, None
        return tenant, ChatThread.objects.filter(tenant=tenant).first()

    @require_platform_admin
    def get(self, request, pk):
        tenant, thread = self._thread(pk)
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if thread is None:
            return Response({"data": {"thread": None, "messages": [], "tenant": tenant.name}})
        messages = list(thread.messages.select_related("actor"))
        unread = sum(1 for m in messages if m.side == "TENANT" and not m.read_by_apex)
        return Response(
            {
                "data": {
                    "thread": thread_row(thread, messages[-1] if messages else None, unread, len(messages)),
                    "messages": [message_row(message) for message in messages],
                    "tenant": tenant.name,
                }
            }
        )

    @require_platform_admin
    def post(self, request, pk):
        tenant, thread = self._thread(pk)
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        # Checked before open_thread(): that helper re-opens, so asking it for a
        # closed thread would silently reverse the close Apex just performed.
        if thread is not None and thread.status != "OPEN":
            return Response(
                {"errors": {"message": "Re-open this conversation before replying."}},
                status=400,
            )
        body = (request.data.get("body") or "").strip()
        image_url = (request.data.get("image_url") or "").strip()
        if not body and not image_url:
            return Response(
                {"errors": {"message": "Write a message before sending it."}}, status=400
            )
        thread = open_thread(tenant, (request.data.get("subject") or "").strip(), request.user)
        message = ChatMessage.objects.create(
            thread=thread, side="APEX", body=body, image_url=image_url, actor=request.user,
            read_by_apex=True,
        )
        log(request.user, "CHAT_REPLIED", "chat_thread", thread.pk, tenant.name)
        return Response({"data": message_row(message)}, status=201)

    @require_platform_admin
    def patch(self, request, pk):
        from django.utils import timezone

        tenant, thread = self._thread(pk)
        if thread is None:
            return Response(
                {"errors": {"message": "This wholesaler has no conversation yet."}},
                status=404,
            )
        action = (request.data.get("action") or request.data.get("status") or "").upper()
        if action in ("CLOSE", "CLOSED"):
            thread.status = "CLOSED"
            thread.closed_by = request.user
            thread.closed_at = timezone.now()
            thread.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])
            log(request.user, "CHAT_CLOSED", "chat_thread", thread.pk, tenant.name)
        elif action in ("REOPEN", "OPEN"):
            thread.status = "OPEN"
            thread.closed_by = None
            thread.closed_at = None
            thread.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])
            log(request.user, "CHAT_REOPENED", "chat_thread", thread.pk, tenant.name)
        elif action == "READ":
            updated = thread.messages.filter(side="TENANT", read_by_apex=False).update(
                read_by_apex=True
            )
            log(request.user, "CHAT_READ", "chat_thread", thread.pk, f"{updated} messages")
        else:
            return Response(
                {"errors": {"message": "action must be close, reopen or read."}}, status=400
            )
        latest = thread.messages.order_by("-created_at", "-id").first()
        return Response(
            {
                "data": thread_row(
                    thread,
                    latest,
                    thread.messages.filter(side="TENANT", read_by_apex=False).count(),
                    thread.messages.count(),
                )
            }
        )


class ChatBroadcastView(APIView):
    """One APEX message into many threads at once.

    {body, subject?, tenant_ids? | all: true}. Per-tenant failures are
    collected and reported instead of aborting the whole broadcast.
    """

    @require_platform_admin
    def post(self, request):
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response(
                {"errors": {"message": "Write the announcement first."}}, status=400
            )
        subject = (request.data.get("subject") or "").strip()
        image_url = (request.data.get("image_url") or "").strip()
        tenant_ids = request.data.get("tenant_ids") or request.data.get("tenants") or []
        if isinstance(tenant_ids, (str, int)):
            tenant_ids = [tenant_ids]
        wanted = [str(pk) for pk in tenant_ids if str(pk).strip()]

        tenants = Tenant.objects.all()
        if wanted:
            tenants = tenants.filter(pk__in=wanted)
        elif not (
            str(request.data.get("all", "")).lower() in ("1", "true", "yes")
        ):
            return Response(
                {"errors": {"message": "Pick tenants or send to all wholesalers."}},
                status=400,
            )
        tenants = list(tenants.order_by("name")[:BROADCAST_LIMIT])
        if not tenants:
            return Response({"errors": {"message": "No wholesaler matched."}}, status=400)

        delivered, failed = [], []
        for tenant in tenants:
            try:
                thread = open_thread(tenant, subject, request.user)
                ChatMessage.objects.create(
                    thread=thread,
                    side="APEX",
                    body=body,
                    image_url=image_url,
                    actor=request.user,
                    read_by_apex=True,
                )
                delivered.append({"tenant": tenant.name, "thread": thread.pk})
            except Exception as error:  # one bad tenant must not stop the rest
                failed.append({"tenant": tenant.name, "message": str(error)})
        log(
            request.user,
            "CHAT_BROADCAST",
            "chat_thread",
            "",
            f"{len(delivered)} sent · {len(failed)} failed",
        )
        return Response(
            {
                "data": {
                    "delivered": len(delivered),
                    "failed": len(failed),
                    "rows": delivered,
                    "errors": failed,
                    "limit": BROADCAST_LIMIT,
                }
            },
            status=201 if delivered else 400,
        )
