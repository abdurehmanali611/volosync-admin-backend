"""Apex Excel seeding: downloadable templates and row-by-row imports.

The console's Excel tab uploads one workbook for one wholesaler. Every row an
import accepts becomes a real, pre-authorized document (status APPROVED, with
its lots/movements applied) and the call is audited as EXCEL_IMPORT — nothing
is left in a queue for the wholesaler to approve.
"""

import json
import re
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import (
    RegistrationLine,
    RegistrationRequest,
    StockAssignment,
    StockLot,
    StockOutRequest,
    next_voucher,
)
from sales.models import SalesAgent
from user.models import Profile
from user.platform import require_platform_admin

from .models import Tenant
from .platform_views import log, truthy

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

INVENTORY_ONLY = "This wholesaler does not have the Inventory module."
SALES_ONLY = "This wholesaler does not have the sales role enabled."


def col(key, header, type_="text", required=False, default="", options=(), help_text=""):
    return {
        "key": key,
        "header": header,
        "type": type_,
        "required": required,
        "default": default,
        "options": list(options),
        "help": help_text,
    }


KINDS = {
    "item_registration": {
        "name": "Item registrations",
        "description": (
            "Purchases that become stock. Rows sharing one supplier are grouped "
            "into a single registration voucher."
        ),
        "requires": lambda tenant: tenant.module_inventory,
        "requires_message": INVENTORY_ONLY,
        "columns": [
            col("item_name", "Item name", required=True),
            col("category", "Category", help_text="Blank groups the item on its own."),
            col("unit", "Measured by", default="PCS", help_text="PCS, KG, LITRE, CARTON…"),
            col("quantity", "Quantity", "number", required=True),
            col("unit_cost", "Unit cost", "money", required=True),
            col("unit_sales", "Unit selling price", "money", required=True),
            col("vat_rate", "VAT %", "number", default="15.00"),
            col("registration_date", "Registration date", "date", required=True),
            col("expiry_date", "Expiry date", "date"),
            col("supplier_name", "Supplier name", required=True),
            col("supplier_phone", "Supplier phone"),
            col("supplier_address", "Supplier address"),
            col("supplier_tin", "Supplier TIN"),
            col("note", "Note"),
        ],
        "samples": [
            [
                "Teft 500ml", "Beverages", "PCS", 240, 42, 60, "15.00",
                "2026-03-04", "2026-09-04", "QA Supplier PLC", "0911000000",
                "Addis Ababa", "1234567890", "Entered from the console",
            ],
        ],
        "notes": [
            "Dates are YYYY-MM-DD; a real Excel date cell also works.",
            "Money and quantity are plain numbers — 1,234.50 reads as 1234.50.",
            "Unit cost is what the supplier was paid per piece, before VAT.",
            "Unit selling price is the shelf price before VAT; VAT % is per line.",
            "A row repeating an existing voucher of the same item, quantity, unit "
            "cost and registration date is skipped as a duplicate.",
        ],
    },
    "stockout_request": {
        "name": "Stock outs",
        "description": (
            "Leaves the store: damage, expiry, supplier returns, free samples. "
            "Each row is its own approved voucher and reduces the lot on hand."
        ),
        "requires": lambda tenant: tenant.module_inventory,
        "requires_message": INVENTORY_ONLY,
        "columns": [
            col(
                "voucher_number",
                "Lot voucher",
                help_text="The voucher printed on the lot. Fill either this or Item name.",
            ),
            col("item_name", "Item name", help_text="Uses that item's newest lot with stock left."),
            col("quantity", "Quantity", "number", required=True),
            col("date", "Movement date", "date", required=True),
            col("change_reason", "Reason / stakeholder", required=True),
            col("changing_amount", "Value given up", "money", default="0.00"),
            col("sales_agent", "Sales agent", help_text="Optional; match by full name."),
            col("note", "Note"),
        ],
        "samples": [
            [
                "REG-2026-000001-001", "Teft 500ml", 12, "2026-03-21",
                "Damaged in transit", "720.00", "", "Entered from the console",
            ],
        ],
        "notes": [
            "Give either the lot voucher or the item name; the voucher wins.",
            "Quantity must fit what is still on the lot — over-drafts are skipped.",
            "Value given up is the selling value lost (0 for a plain stock out).",
        ],
    },
    "sales_agent": {
        "name": "Sales agents",
        "description": (
            "Agents on the wholesaler's shared Sales login. No per-agent username "
            "or password is created."
        ),
        "requires": lambda tenant: tenant.sales_role_enabled,
        "requires_message": SALES_ONLY,
        "columns": [
            col("full_name", "Full name", required=True),
            col("phone", "Phone"),
            col("sex", "Sex", "choice", options=("Female", "Male")),
            col("years_experience", "Years of experience", "int", default="0"),
            col("active", "Active", "bool", default="TRUE", options=("TRUE", "FALSE")),
        ],
        "samples": [["QA Agent", "0911000000", "Female", 3, "TRUE"]],
        "notes": [
            "The wholesaler must already have its Sales account in Access; agents "
            "are records on that shared login.",
            "Years of experience is clamped to 0-80.",
        ],
    },
}


def kind_row(kind):
    spec = KINDS[kind]
    return {
        "kind": kind,
        "name": spec["name"],
        "description": spec["description"],
        "columns": [
            {
                "key": column["key"],
                "header": column["header"],
                "required": column["required"],
                "type": column["type"],
                "default": column["default"],
                "options": column["options"],
                "help": column["help"],
            }
            for column in spec["columns"]
        ],
        "samples": spec["samples"],
        "notes": spec["notes"],
    }


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _header_map(kind):
    """Every spelling a header cell may use → the column key."""
    map_ = {}
    for column in KINDS[kind]["columns"]:
        for label in (column["key"], column["header"], column["header"].split("(")[0]):
            key = _norm(label)
            if key:
                map_[key] = column["key"]
    return map_


class RowError(Exception):
    pass


def _text(value):
    return "" if value is None else str(value).strip()


def _blank(value):
    return value is None or _text(value) == ""


def _number(value, label):
    text = _text(value).replace(",", "").replace(" ", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise RowError(f"{label} must be a number, not {_text(value)}.")


def _money(value, label, default=None):
    if _blank(value):
        if default is None:
            raise RowError(f"{label} is required.")
        return Decimal(str(default)).quantize(Decimal("0.01"))
    return _number(value, label).quantize(Decimal("0.01"))


def _date(value, label):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date_type):
        return value
    text = _text(value)
    parsed = parse_date(text[:10]) or parse_date(text.replace("/", "-")[:10])
    if parsed is None:
        raise RowError(f"{label} must be a YYYY-MM-DD date, not {text}.")
    return parsed


def _choice(value, label, options):
    text = _text(value)
    if not text:
        return ""
    for option in options:
        if _norm(option) == _norm(text):
            return option
    raise RowError(f"{label} must be one of: {', '.join(options)}.")


def coerce(kind, raw):
    """One row of cells → typed values, or RowError naming the first complaint."""
    out = {}
    for column in KINDS[kind]["columns"]:
        key, label, type_ = column["key"], column["header"], column["type"]
        value = raw.get(key)
        if column["required"] and _blank(value):
            raise RowError(f"{label} is required.")
        if type_ == "number":
            out[key] = None if _blank(value) else _number(value, label)
        elif type_ == "money":
            out[key] = _money(value, label, column["default"] or None)
        elif type_ == "int":
            out[key] = int(_number(value, label)) if not _blank(value) else int(column["default"] or 0)
        elif type_ == "date":
            out[key] = None if _blank(value) else _date(value, label)
        elif type_ == "bool":
            out[key] = column["default"] == "TRUE" if _blank(value) else truthy(value)
        elif type_ == "choice":
            out[key] = _choice(value, label, column["options"])
        else:
            out[key] = _text(value)
    return out


# --------------------------------------------------------------------- import


def _sheet_rows(kind, sheet):
    """Cells → [{column key: value, "_row": excel row number}]."""
    map_ = _header_map(kind)
    grid = [list(cells) for cells in sheet.iter_rows(values_only=True)]
    header_index = None
    keys = []
    for index, cells in enumerate(grid):
        matched = [map_.get(_norm(cell)) for cell in cells]
        if sum(1 for key in matched if key) >= 2:
            header_index, keys = index, matched
            break
    if header_index is None:
        raise RowError("No header row found — start from the downloaded template.")
    rows = []
    for offset, cells in enumerate(grid[header_index + 1 :], start=1):
        if all(_blank(cell) for cell in cells):
            continue
        raw = {
            key: cells[position]
            for position, key in enumerate(keys)
            if key and position < len(cells)
        }
        raw["_row"] = header_index + offset + 1
        rows.append(raw)
    return rows


def _json_rows(kind, payload):
    """Client-parsed rows (JSON) get the same header matching as a file."""
    map_ = _header_map(kind)
    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise RowError(f"Row {index + 1} is not an object of column values.")
        raw = {
            map_[_norm(label)]: value
            for label, value in row.items()
            if _norm(label) in map_
        }
        raw["_row"] = index + 2
        rows.append(raw)
    return rows


def _lot_already_registered(tenant, values):
    return StockLot.objects.filter(
        tenant=tenant,
        item__name__iexact=values["item_name"],
        item__category__iexact=values["category"],
        quantity=values["quantity"],
        unit_cost=values["unit_cost"],
        registration_date=values["registration_date"],
    ).exists()


def _import_registrations(tenant, actor, rows, dry_run):
    imported, errors, documents = 0, [], []
    groups = {}
    seen = {}
    for raw in rows:
        row_num = raw["_row"]
        try:
            values = coerce("item_registration", raw)
            if values["quantity"] <= 0:
                raise RowError("Quantity must be more than zero.")
            if values["unit_cost"] < 0 or values["unit_sales"] < 0:
                raise RowError("Unit cost and selling price cannot be negative.")
            if values["expiry_date"] and values["expiry_date"] < values["registration_date"]:
                raise RowError("Expiry date is before the registration date.")
        except RowError as exc:
            errors.append({"row": row_num, "message": str(exc)})
            continue
        signature = (
            values["item_name"].lower(),
            values["category"].lower(),
            str(values["quantity"]),
            str(values["unit_cost"]),
            str(values["registration_date"]),
        )
        if signature in seen:
            errors.append(
                {"row": row_num, "message": f"Same item and quantity as row {seen[signature]}."}
            )
            continue
        seen[signature] = row_num
        if not dry_run and _lot_already_registered(tenant, values):
            errors.append(
                {"row": row_num, "message": "That voucher is already registered here."}
            )
            continue
        group_key = (
            values["supplier_name"],
            values["supplier_phone"],
            values["supplier_tin"],
        )
        groups.setdefault(group_key, []).append((row_num, values))

    for group_key, group in groups.items():
        if dry_run:
            imported += len(group)
            continue
        try:
            with transaction.atomic():
                doc = RegistrationRequest.objects.create(
                    tenant=tenant,
                    voucher_number=next_voucher(RegistrationRequest, tenant, "REG"),
                    status="APPROVED",
                    created_by=actor,
                    submitted_at=timezone.now(),
                    supplier_name=group_key[0],
                    supplier_phone=group_key[1],
                    supplier_tin=group_key[2],
                    supplier_address=group[0][1]["supplier_address"],
                    note="Apex Excel seed",
                )
                for _row_num, values in group:
                    RegistrationLine.objects.create(
                        request=doc,
                        item_name=values["item_name"],
                        category=values["category"],
                        unit=values["unit"] or "PCS",
                        quantity=values["quantity"],
                        unit_cost=values["unit_cost"],
                        unit_sales=values["unit_sales"],
                        vat_rate=values["vat_rate"]
                        if values["vat_rate"] is not None
                        else tenant.vat_rate,
                        registration_date=values["registration_date"],
                        expiry_date=values["expiry_date"],
                        note=values["note"],
                    )
                doc.apply_on_approval()
            imported += len(group)
            documents.append({"reference": doc.voucher_number, "lines": len(group)})
        except Exception as exc:  # one bad supplier group must not lose the others
            for row_num, _values in group:
                errors.append({"row": row_num, "message": f"Could not register: {exc}"})
    return imported, errors, documents


def _import_stockouts(tenant, actor, rows, dry_run):
    imported, errors, documents = 0, [], []
    for raw in rows:
        row_num = raw["_row"]
        try:
            values = coerce("stockout_request", raw)
            if values["quantity"] <= 0:
                raise RowError("Quantity must be more than zero.")
            if not values["voucher_number"] and not values["item_name"]:
                raise RowError("Give either the lot voucher or the item name.")
            lots = StockLot.objects.filter(tenant=tenant)
            if values["voucher_number"]:
                lots = lots.filter(voucher_number=values["voucher_number"])
            else:
                lots = lots.filter(item__name__iexact=values["item_name"])
            lot = lots.order_by("-registration_date", "-id").first()
            if lot is None:
                raise RowError(f"No lot matches {values['voucher_number'] or values['item_name']}.")
            if values["quantity"] > lot.remaining_quantity:
                raise RowError(
                    f"{lot.voucher_number} only has {lot.remaining_quantity} left."
                )
            agent = None
            if values["sales_agent"]:
                agent = SalesAgent.objects.filter(
                    tenant=tenant, first_name__icontains=values["sales_agent"].split()[0]
                ).first()
            if dry_run:
                imported += 1
                continue
            with transaction.atomic():
                doc = StockOutRequest.objects.create(
                    tenant=tenant,
                    voucher_number=next_voucher(StockOutRequest, tenant, "SO"),
                    mode="SINGLE",
                    date=values["date"],
                    status="APPROVED",
                    created_by=actor,
                    note=values["note"] or "Apex Excel seed",
                )
                StockAssignment.objects.create(
                    request=doc,
                    lot=lot,
                    sales_agent=agent,
                    quantity=values["quantity"],
                    changing_amount=values["changing_amount"],
                    change_reason=values["change_reason"],
                    unit_sales_at_assignment=lot.unit_sales,
                )
                doc.apply_on_approval()
            imported += 1
            documents.append({"reference": doc.voucher_number, "lines": 1})
        except RowError as exc:
            errors.append({"row": row_num, "message": str(exc)})
        except Exception as exc:
            errors.append({"row": row_num, "message": f"Could not stock out: {exc}"})
    return imported, errors, documents


def _import_agents(tenant, actor, rows, dry_run):
    imported, errors, documents = 0, [], []
    sales_login = ""
    if not dry_run:
        profile = (
            Profile.objects.filter(tenant=tenant, role="SALES")
            .select_related("user")
            .first()
        )
        if profile is None:
            raise RowError(
                "Create the Sales account in Access first — agents belong to that shared login."
            )
        sales_login = profile.user.username
    for raw in rows:
        row_num = raw["_row"]
        try:
            values = coerce("sales_agent", raw)
            parts = values["full_name"].split()
            if len(parts) < 2:
                raise RowError("Full name needs a first and a last name.")
            first, last = parts[0], " ".join(parts[1:])
            if SalesAgent.objects.filter(
                tenant=tenant, first_name__iexact=first, last_name__iexact=last,
                phone=values["phone"],
            ).exists():
                raise RowError("That agent is already registered.")
            if dry_run:
                imported += 1
                continue
            agent = SalesAgent.objects.create(
                tenant=tenant,
                login_username=sales_login,
                first_name=first,
                last_name=last,
                phone=values["phone"],
                sex={"Female": "F", "Male": "M"}.get(values["sex"], ""),
                years_experience=min(max(values["years_experience"], 0), 80),
                is_active=values["active"],
            )
            imported += 1
            documents.append({"reference": agent.full_name, "lines": 1})
        except RowError as exc:
            errors.append({"row": row_num, "message": str(exc)})
        except Exception as exc:
            errors.append({"row": row_num, "message": f"Could not add the agent: {exc}"})
    return imported, errors, documents


IMPORTERS = {
    "item_registration": _import_registrations,
    "stockout_request": _import_stockouts,
    "sales_agent": _import_agents,
}


# --------------------------------------------------------------------- views


class ExcelKindListView(APIView):
    """What the Excel tab can import, with each sheet's columns."""

    @require_platform_admin
    def get(self, request):
        return Response(
            {
                "data": [
                    {**kind_row(kind), "max_rows": MAX_ROWS, "max_bytes": MAX_BYTES}
                    for kind in KINDS
                ]
            }
        )


def _guard(tenant, kind):
    if kind not in KINDS:
        return Response(
            {
                "errors": {
                    "message": f"Unknown sheet: {kind or '(none given)'}. "
                    f"We have {', '.join(KINDS)}."
                }
            },
            status=400,
        )
    spec = KINDS[kind]
    if not spec["requires"](tenant):
        return Response({"errors": {"message": spec["requires_message"]}}, status=400)
    return None


class ExcelTemplateView(APIView):
    """GET ?kind= — a workbook with tips, headers, sample rows and a legend."""

    @require_platform_admin
    def get(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        kind = _text(request.query_params.get("kind"))
        error = _guard(tenant, kind)
        if error:
            return error

        spec = KINDS[kind]
        book = Workbook()
        sheet = book.active
        sheet.title = spec["name"][:31]
        banner = sheet.cell(
            row=1,
            column=1,
            value=spec["description"] + " Replace the sample row before you upload.",
        )
        banner.font = Font(bold=True)
        banner.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.merge_cells(
            start_row=1, start_column=1, end_row=1, end_column=len(spec["columns"])
        )
        sheet.row_dimensions[1].height = 34
        sheet.append([column["header"] for column in spec["columns"]])
        for cell in sheet[2]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="EEF2F7")
        for sample in spec["samples"]:
            sheet.append(sample)
        for index, column in enumerate(spec["columns"], start=1):
            sheet.column_dimensions[sheet.cell(row=2, column=index).column_letter].width = (
                max(14, min(38, len(column["header"]) + 8))
            )

        allowed = book.create_sheet("Allowed values")
        allowed.append(["Column", "Allowed values"])
        for cell in allowed[1]:
            cell.font = Font(bold=True)
        for column in spec["columns"]:
            options = ", ".join(column["options"])
            allowed.append([column["header"], options or column["help"]])
        allowed.column_dimensions["A"].width = 26
        allowed.column_dimensions["B"].width = 70

        notes = book.create_sheet("Instructions")
        notes.append(["How this sheet is imported"])
        notes[1][0].font = Font(bold=True)
        for line in spec["notes"]:
            notes.append([line])
        notes.append([f"Up to {MAX_ROWS} rows per file."])
        notes.column_dimensions["A"].width = 90

        stream = BytesIO()
        book.save(stream)
        return HttpResponse(
            stream.getvalue(),
            content_type=XLSX,
            headers={
                "Content-Disposition": f'attachment; filename="volosync-{tenant.tin}-{kind}.xlsx"'
            },
        )


class ExcelImportView(APIView):
    """POST a workbook (multipart) or parsed rows (JSON) for one wholesaler.

    {kind, file | rows, dry_run?} → per-row errors; a bad row never aborts the
    file, it is reported as {row, message} and skipped.
    """

    @require_platform_admin
    def post(self, request, pk):
        tenant = Tenant.objects.filter(pk=pk).first()
        if tenant is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        kind = _text(request.data.get("kind"))
        error = _guard(tenant, kind)
        if error:
            return error
        dry_run = truthy(request.data.get("dry_run"))

        upload = request.data.get("file")
        try:
            if hasattr(upload, "read"):
                if upload.size > MAX_BYTES:
                    return Response(
                        {"errors": {"message": "That file is larger than 5 MB."}}, status=400
                    )
                book = load_workbook(BytesIO(upload.read()), data_only=True)
                rows = []
                for sheet in book.worksheets:
                    try:
                        rows.extend(_sheet_rows(kind, sheet))
                    except RowError:
                        # Side sheets (Allowed values, Instructions) carry no header.
                        continue
                if not rows:
                    return Response(
                        {"errors": {"message": "That sheet has no data rows."}}, status=400
                    )
            else:
                payload = request.data.get("rows")
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, list) or not payload:
                    return Response(
                        {"errors": {"message": "Attach a file or send rows[] to import."}},
                        status=400,
                    )
                rows = _json_rows(kind, payload)
        except RowError as exc:
            return Response({"errors": {"message": str(exc)}}, status=400)
        except (ValueError, TypeError):
            return Response(
                {"errors": {"message": "rows[] must be a JSON list of column values."}},
                status=400,
            )
        except Exception as exc:
            return Response(
                {"errors": {"message": f"That workbook could not be read: {exc}"}}, status=400
            )

        if len(rows) > MAX_ROWS:
            return Response(
                {
                    "errors": {
                        "message": f"{len(rows)} rows is over the {MAX_ROWS} row limit — "
                        "split the file."
                    }
                },
                status=400,
            )

        try:
            imported, errors, documents = IMPORTERS[kind](tenant, request.user, rows, dry_run)
        except RowError as exc:
            return Response({"errors": {"message": str(exc)}}, status=400)
        errors.sort(key=lambda item: item["row"])

        if not dry_run:
            detail = (
                f"{tenant.name} · {KINDS[kind]['name']} · {imported} rows"
                + (f" · {len(errors)} skipped" if errors else "")
            )
            log(request.user, "EXCEL_IMPORT", "tenant", tenant.pk, detail)

        return Response(
            {
                "data": {
                    "kind": kind,
                    "sheet": KINDS[kind]["name"],
                    "dry_run": dry_run,
                    "tenant": tenant.name,
                    "rows": len(rows),
                    "imported_count": imported,
                    "skipped_count": len(errors),
                    "documents": documents,
                    "errors": errors,
                }
            }
        )
