from collections import defaultdict
from datetime import datetime, timedelta
from uuid import uuid4

import boto3
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.response import Response

from .api import APIView
from .models import (
    Advance,
    BaseProduct,
    Client,
    ClientEmail,
    ClientPhone,
    ColourChange,
    Company,
    Deal,
    Description,
    Drawing,
    Lead,
    Notification,
    NotificationRecipient,
    Order,
    Product,
    Quotation,
    QuotationItem,
    QuotationProduct,
    QuotationWorking,
    Source,
    TenantMembership,
    User,
)
from .serializers import (
    BaseProductSerializer,
    ClientSerializer,
    CompanySerializer,
    DealSerializer,
    DescriptionSerializer,
    DrawingSerializer,
    LeadSerializer,
    NotificationRecipientSerializer,
    OrderSerializer,
    ProductSerializer,
    QuotationSerializer,
    SourceSerializer,
    UserSerializer,
)
from .services import resolve_tenant


def tenant_for(request):
    return resolve_tenant(request)


def page_values(request):
    try:
        rows = min(max(int(request.query_params.get("rows", 20)), 1), 200)
        page = max(int(request.query_params.get("page", 1)), 1)
    except ValueError:
        rows, page = 20, 1
    return rows, page


def paginate(queryset, request):
    rows, page = page_values(request)
    return queryset[(page - 1) * rows:page * rows]


def date_filters(request, field="created_at"):
    query = Q()
    start = request.query_params.get("startDate")
    end = request.query_params.get("endDate")
    if start:
        query &= Q(**{f"{field}__date__gte": start[:10]})
    if end:
        query &= Q(**{f"{field}__date__lte": end[:10]})
    return query


def employee_ids(request):
    raw = request.query_params.get("employeeID", "")
    return [int(item) for item in raw.split(",") if item.strip().isdigit()]


def source_ids(request):
    raw = request.query_params.get("sources", "")
    return [int(item) for item in raw.split(",") if item.strip().isdigit()]


def replace_client_contacts(client, tenant, phones, emails):
    client.phones.all().delete()
    client.emails.all().delete()
    ClientPhone.objects.bulk_create([
        ClientPhone(
            tenant=tenant, client=client,
            phone=item.get("number") or item.get("phone") or "",
        )
        for item in phones
    ])
    ClientEmail.objects.bulk_create([
        ClientEmail(tenant=tenant, client=client, email=item.get("email") or None)
        for item in (emails or [])
    ])


class EmployeeListView(APIView):
    def get(self, request, sales_only=False):
        tenant = tenant_for(request)
        users = User.objects.filter(
            tenant_memberships__tenant=tenant,
            tenant_memberships__is_active=True,
            is_active=True,
        ).distinct()
        if sales_only:
            users = users.filter(department=User.Department.SALES)
        return Response({
            "message": "Employees fetched successfully",
            "employees": UserSerializer(users, many=True).data,
        })


class SalesEmployeeListView(EmployeeListView):
    def get(self, request):
        return super().get(request, sales_only=True)


class AssignedEmployeeView(APIView):
    def get(self, request, ref_id):
        tenant = tenant_for(request)
        object_type = request.query_params.get("type", "lead")
        obj = (
            Deal.objects.filter(tenant=tenant, pk=ref_id).first()
            if object_type == "deal"
            else Lead.objects.filter(tenant=tenant, pk=ref_id).first()
        )
        users = obj.assigned_to.all() if obj else User.objects.none()
        return Response({
            "message": "Assigned employees fetched successfully",
            "employees": UserSerializer(users, many=True).data,
        })


class LookupListCreateView(APIView):
    model = None
    serializer_class = None
    response_key = ""

    def get(self, request):
        tenant = tenant_for(request)
        objects = self.model.objects.filter(tenant=tenant).order_by("name")
        return Response({
            "message": f"{self.response_key.title()} fetched successfully",
            self.response_key: self.serializer_class(objects, many=True).data,
        })

    def post(self, request):
        tenant = tenant_for(request)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj = serializer.save(tenant=tenant)
        return Response(
            {"message": f"{self.model.__name__} added successfully",
             self.model.__name__.lower(): self.serializer_class(obj).data}
        )

    def put(self, request, pk):
        tenant = tenant_for(request)
        obj = self.model.objects.filter(tenant=tenant, pk=pk).first()
        if not obj:
            return Response({"message": "Not found"}, status=404)
        serializer = self.serializer_class(obj, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"message": f"{self.model.__name__} edited successfully"})


class SourceView(LookupListCreateView):
    model = Source
    serializer_class = SourceSerializer
    response_key = "sources"


class ProductView(LookupListCreateView):
    model = Product
    serializer_class = ProductSerializer
    response_key = "products"


class CompanyListView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        companies = Company.objects.filter(tenant=tenant)
        name = request.query_params.get("name")
        if name:
            companies = companies.filter(name__icontains=name)
        return Response({
            "message": "Companies fetched successfully",
            "companies": CompanySerializer(companies, many=True).data,
        })


class CompanyClientView(APIView):
    def get(self, request, company_id):
        tenant = tenant_for(request)
        clients = Client.objects.filter(
            tenant=tenant, company_id=company_id
        ).prefetch_related("emails", "phones")
        return Response({
            "message": "Company employee fetched successfully",
            "employees": ClientSerializer(clients, many=True).data,
        })

    @transaction.atomic
    def post(self, request, company_id):
        tenant = tenant_for(request)
        company = Company.objects.filter(tenant=tenant, pk=company_id).first()
        if not company:
            return Response({"message": "Company not found"}, status=404)
        if not request.data.get("first_name") or not request.data.get("phones"):
            return Response({"message": "Input validation error"}, status=400)
        client = Client.objects.create(
            tenant=tenant, company=company,
            first_name=request.data["first_name"],
            last_name=request.data.get("last_name"),
        )
        replace_client_contacts(
            client, tenant, request.data["phones"], request.data.get("emails", [])
        )
        return Response({"message": "Client details added successfully"})

    @transaction.atomic
    def put(self, request, company_id):
        tenant = tenant_for(request)
        client = Client.objects.filter(
            tenant=tenant, company_id=company_id, pk=request.data.get("id")
        ).first()
        if not client:
            return Response({"message": "Client not found"}, status=404)
        client.first_name = request.data["first_name"]
        client.last_name = request.data.get("last_name")
        client.save()
        replace_client_contacts(
            client, tenant, request.data.get("phones", []),
            request.data.get("emails", []),
        )
        return Response({"message": "Client details edited successfully"})


class CompanyDetailView(APIView):
    def put(self, request, company_id):
        tenant = tenant_for(request)
        company = Company.objects.filter(tenant=tenant, pk=company_id).first()
        if not company:
            return Response({"message": "Company not found"}, status=404)
        company.name = request.data.get("company_name", company.name)
        company.address = request.data.get("address", company.address)
        company.gst_no = request.data.get("gst_no")
        company.save()
        return Response({"message": "Company details edited successfully"})


class LeadListCreateView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        leads = Lead.objects.filter(tenant=tenant).select_related(
            "company", "client_detail", "source", "product"
        ).prefetch_related("assigned_to", "client_detail__emails", "client_detail__phones")
        search = request.query_params.get("search")
        if search:
            leads = leads.filter(
                Q(company__name__icontains=search)
                | Q(client_detail__first_name__icontains=search)
                | Q(client_detail__last_name__icontains=search)
            )
        ids = employee_ids(request)
        if ids:
            leads = leads.filter(assigned_to__id__in=ids)
        sources = source_ids(request)
        if sources:
            leads = leads.filter(source_id__in=sources)
        leads = leads.filter(date_filters(request)).distinct().order_by("-created_at")
        total = leads.count()
        return Response({
            "message": "Leads fetched successfully",
            "leads": LeadSerializer(paginate(leads, request), many=True).data,
            "totalLeads": total,
        })

    @transaction.atomic
    def post(self, request):
        tenant = tenant_for(request)
        required = ("first_name", "phones", "assigned_to", "source_id", "product_id",
                    "company_name", "address")
        if any(not request.data.get(key) for key in required):
            return Response({"message": "Input validation error"}, status=400)
        source = Source.objects.filter(tenant=tenant, pk=request.data["source_id"]).first()
        product = Product.objects.filter(tenant=tenant, pk=request.data["product_id"]).first()
        assignees = User.objects.filter(
            tenant_memberships__tenant=tenant,
            id__in=[item["id"] for item in request.data["assigned_to"]],
        )
        if not source or not product or assignees.count() != len(request.data["assigned_to"]):
            return Response({"message": "Invalid tenant-scoped relation"}, status=400)
        company = Company.objects.create(
            tenant=tenant, name=request.data["company_name"],
            address=request.data["address"], gst_no=request.data.get("gst_no"),
        )
        client = Client.objects.create(
            tenant=tenant, company=company, first_name=request.data["first_name"],
            last_name=request.data.get("last_name"),
        )
        replace_client_contacts(
            client, tenant, request.data["phones"], request.data.get("emails", [])
        )
        lead = Lead.objects.create(
            tenant=tenant, company=company, client_detail=client,
            source=source, product=product,
        )
        lead.assigned_to.set(assignees)
        create_assignment_notification(
            tenant, "lead_assigned", "Lead assigned", f"Lead {lead.pk} assigned",
            assignees, lead=lead,
        )
        return Response({"message": "Lead added successfully", "lead_id": lead.pk})


class LeadDetailView(APIView):
    def get(self, request, lead_id):
        tenant = tenant_for(request)
        lead = Lead.objects.filter(tenant=tenant, pk=lead_id).select_related(
            "company", "client_detail", "source", "product"
        ).prefetch_related("assigned_to", "client_detail__emails", "client_detail__phones").first()
        return Response({
            "message": "Lead fetched successfully",
            "lead": LeadSerializer(lead).data if lead else None,
        }, status=200 if lead else 404)

    @transaction.atomic
    def put(self, request, lead_id):
        tenant = tenant_for(request)
        lead = Lead.objects.select_for_update().filter(tenant=tenant, pk=lead_id).first()
        if not lead:
            return Response({"message": "Lead not found"}, status=404)
        lead.company.name = request.data.get("company_name", lead.company.name)
        lead.company.address = request.data.get("address", lead.company.address)
        lead.company.gst_no = request.data.get("gst_no")
        lead.company.save()
        lead.client_detail.first_name = request.data.get(
            "first_name", lead.client_detail.first_name
        )
        lead.client_detail.last_name = request.data.get("last_name")
        lead.client_detail.save()
        if "phones" in request.data:
            replace_client_contacts(
                lead.client_detail, tenant, request.data["phones"],
                request.data.get("emails", []),
            )
        for field in ("source_id", "product_id"):
            if field in request.data:
                model = Source if field == "source_id" else Product
                if not model.objects.filter(
                    tenant=tenant, pk=request.data[field]
                ).exists():
                    return Response({"message": "Invalid tenant-scoped relation"}, status=400)
                setattr(lead, field, request.data[field])
        lead.save()
        if "assigned_to" in request.data:
            users = User.objects.filter(
                tenant_memberships__tenant=tenant,
                id__in=[item["id"] for item in request.data["assigned_to"]],
            )
            lead.assigned_to.set(users)
        return Response({"message": "Lead edited successfully"})


class AnalyticsView(APIView):
    model = None
    assignment_field = "assigned_to"
    response_total = ""
    response_counts = ""

    def get(self, request, duration):
        tenant = tenant_for(request)
        qs = self.model.objects.filter(tenant=tenant)
        days = {"week": 7, "month": 30, "year": 365}.get(duration)
        if days:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
        counts = {
            f"{row['assigned_to__first_name']} {row['assigned_to__last_name']}":
                row["count"]
            for row in qs.values(
                "assigned_to__first_name", "assigned_to__last_name"
            ).annotate(count=Count("id"))
            if row["assigned_to__first_name"]
        }
        return Response({
            "message": "Analytics fetched successfully",
            self.response_total: qs.distinct().count(),
            self.response_counts: counts,
        })


class LeadAnalyticsView(AnalyticsView):
    model = Lead
    response_total = "totalLeads"
    response_counts = "employeeLeadCount"


def create_assignment_notification(
    tenant, notification_type, title, message, users, **relations
):
    notification = Notification.objects.create(
        tenant=tenant, type=notification_type, title=title, message=message, **relations
    )
    NotificationRecipient.objects.bulk_create([
        NotificationRecipient(tenant=tenant, notification=notification, user=user)
        for user in users
    ])
    return notification


class DealListCreateView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        deals = Deal.objects.filter(tenant=tenant).select_related(
            "company", "client_detail", "source", "product"
        ).prefetch_related("assigned_to", "client_detail__emails", "client_detail__phones")
        search = request.query_params.get("search")
        if search:
            deals = deals.filter(
                Q(pk__icontains=search) | Q(company__name__icontains=search)
            )
        ids = employee_ids(request)
        if ids:
            deals = deals.filter(assigned_to__id__in=ids)
        sources = source_ids(request)
        if sources:
            deals = deals.filter(source_id__in=sources)
        deals = deals.filter(date_filters(request)).distinct().order_by("-created_at")
        total = deals.count()
        return Response({
            "message": "Deals fetched successfully",
            "deals": DealSerializer(paginate(deals, request), many=True).data,
            "totalDeals": total,
        })

    @transaction.atomic
    def post(self, request):
        tenant = tenant_for(request)
        relation_map = {
            "company": Company, "client_detail": Client,
            "source": Source, "product": Product,
        }
        objects = {}
        for name, model in relation_map.items():
            pk = request.data.get(f"{name.replace('_detail', '')}_id")
            obj = model.objects.filter(tenant=tenant, pk=pk).first()
            if not obj:
                return Response({"message": f"Invalid {name}"}, status=400)
            objects[name] = obj
        deal_id = request.data.get("id") or f"D-{tenant.pk}-{uuid4().hex[:8].upper()}"
        lead = None
        if request.data.get("lead_id"):
            lead = Lead.objects.filter(
                tenant=tenant, pk=request.data["lead_id"]
            ).first()
            if not lead:
                return Response({"message": "Invalid lead"}, status=400)
        deal = Deal.objects.create(
            tenant=tenant, id=deal_id,
            deal_status=request.data.get("deal_status", Deal.Status.PENDING),
            updated_by=request.user, lead=lead, **objects,
        )
        users = User.objects.filter(
            tenant_memberships__tenant=tenant,
            id__in=[item["id"] for item in request.data.get("assigned_to", [])],
        )
        deal.assigned_to.set(users)
        create_assignment_notification(
            tenant, "deal_assigned", "Deal assigned", f"Deal {deal.pk} assigned",
            users, deal=deal,
        )
        return Response({"message": "Deal added successfully", "deal_id": deal.pk})


class DealDetailView(APIView):
    def get(self, request, deal_id):
        tenant = tenant_for(request)
        deal = Deal.objects.filter(tenant=tenant, pk=deal_id).select_related(
            "company", "client_detail", "source", "product"
        ).prefetch_related("assigned_to").first()
        return Response({
            "message": "Deal fetched successfully",
            "deal": DealSerializer(deal).data if deal else None,
        }, status=200 if deal else 404)

    def put(self, request, deal_id):
        tenant = tenant_for(request)
        deal = Deal.objects.filter(tenant=tenant, pk=deal_id).first()
        if not deal:
            return Response({"message": "Deal not found"}, status=404)
        if "deal_status" in request.data:
            deal.deal_status = request.data["deal_status"]
        relation_fields = (
            ("company_id", "company_id", Company),
            ("client_id", "client_detail_id", Client),
            ("source_id", "source_id", Source),
            ("product_id", "product_id", Product),
        )
        for data_key, field, model in relation_fields:
            if data_key in request.data:
                if not model.objects.filter(
                    tenant=tenant, pk=request.data[data_key]
                ).exists():
                    return Response(
                        {"message": f"Invalid tenant-scoped {data_key}"}, status=400
                    )
                setattr(deal, field, request.data[data_key])
        deal.updated_by = request.user
        deal.save()
        if "assigned_to" in request.data:
            deal.assigned_to.set(User.objects.filter(
                tenant_memberships__tenant=tenant,
                id__in=[item["id"] for item in request.data["assigned_to"]],
            ))
        return Response({"message": "Deal edited successfully"})


class DealStatusView(APIView):
    def put(self, request, deal_id):
        tenant = tenant_for(request)
        updated = Deal.objects.filter(tenant=tenant, pk=deal_id).update(
            deal_status=request.data.get("status"), updated_by=request.user
        )
        return Response(
            {"message": "Deal status edited successfully"},
            status=200 if updated else 404,
        )


class ConvertLeadView(APIView):
    @transaction.atomic
    def post(self, request, lead_id):
        tenant = tenant_for(request)
        lead = Lead.objects.select_for_update().filter(
            tenant=tenant, pk=lead_id, is_converted=False
        ).first()
        if not lead:
            return Response({"message": "Lead not found or already converted"}, status=404)
        code = request.data.get("quotation_code") or request.user.quotation_code or "D"
        deal_id = f"{code}-{tenant.pk}-{lead.pk}"
        deal = Deal.objects.create(
            tenant=tenant, id=deal_id, deal_status=Deal.Status.PENDING,
            company=lead.company, client_detail=lead.client_detail,
            source=lead.source, product=lead.product, lead=lead,
            updated_by=request.user,
        )
        deal.assigned_to.set(lead.assigned_to.all())
        lead.is_converted = True
        lead.save(update_fields=["is_converted"])
        return Response({"message": "Lead converted successfully", "deal_id": deal.pk})


class DealIdsView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        return Response({
            "message": "Deal IDs fetched successfully",
            "dealIds": list(Deal.objects.filter(tenant=tenant).values("id")),
        })


class DealAnalyticsView(AnalyticsView):
    model = Deal
    response_total = "totalDeals"
    response_counts = "employeeDealCount"


class DescriptionView(APIView):
    def get(self, request, ref_id):
        tenant = tenant_for(request)
        kind = request.query_params.get("type")
        qs = Description.objects.filter(tenant=tenant)
        qs = qs.filter(deal_id=ref_id) if kind == "deal" else qs.filter(lead_id=ref_id)
        return Response({
            "message": "Descriptions fetched successfully",
            "descriptions": DescriptionSerializer(qs, many=True).data,
        })

    def post(self, request, ref_id):
        tenant = tenant_for(request)
        kind = request.data.get("type")
        kwargs = {"deal_id": ref_id} if kind == "deal" else {"lead_id": ref_id}
        parent_model = Deal if kind == "deal" else Lead
        if not parent_model.objects.filter(tenant=tenant, pk=ref_id).exists():
            return Response({"message": "Parent not found"}, status=404)
        description = Description.objects.create(
            tenant=tenant, notes=request.data.get("description", ""),
            updated_by=request.user, **kwargs,
        )
        return Response({
            "message": "Description added successfully",
            "description": DescriptionSerializer(description).data,
        })

    def put(self, request, ref_id):
        tenant = tenant_for(request)
        description = Description.objects.filter(tenant=tenant, pk=ref_id).first()
        if not description:
            return Response({"message": "Description not found"}, status=404)
        description.notes = request.data.get("description", description.notes)
        description.updated_by = request.user
        description.save()
        return Response({"message": "Description edited successfully"})

    def delete(self, request, ref_id):
        tenant = tenant_for(request)
        deleted, _ = Description.objects.filter(tenant=tenant, pk=ref_id).delete()
        return Response(
            {"message": "Description deleted successfully"},
            status=200 if deleted else 404,
        )


class ReminderView(APIView):
    def get(self, request, ref_id):
        tenant = tenant_for(request)
        kind = request.query_params.get("type")
        qs = Notification.objects.filter(tenant=tenant, send_at__isnull=False)
        qs = qs.filter(deal_id=ref_id) if kind == "deal" else qs.filter(lead_id=ref_id)
        from .serializers import NotificationSerializer
        return Response({
            "message": "Reminders fetched successfully",
            "reminders": NotificationSerializer(qs, many=True).data,
        })

    def post(self, request, ref_id):
        tenant = tenant_for(request)
        kind = request.data.get("type")
        kwargs = {"deal_id": ref_id} if kind == "deal" else {"lead_id": ref_id}
        notification = Notification.objects.create(
            tenant=tenant, title=request.data["title"],
            message=request.data.get("message"), send_at=request.data["send_at"],
            type=request.data.get("reminder_type", "client_meeting"), **kwargs,
        )
        NotificationRecipient.objects.create(
            tenant=tenant, notification=notification, user=request.user
        )
        return Response({"message": "Reminder added successfully"})

    def put(self, request, ref_id):
        tenant = tenant_for(request)
        notification = Notification.objects.filter(
            tenant=tenant, pk=ref_id, send_at__isnull=False
        ).first()
        if not notification:
            return Response({"message": "Reminder not found"}, status=404)
        for field in ("title", "message", "send_at"):
            if field in request.data:
                setattr(notification, field, request.data[field])
        notification.save()
        return Response({"message": "Reminder edited successfully"})

    def delete(self, request, ref_id):
        tenant = tenant_for(request)
        deleted, _ = Notification.objects.filter(
            tenant=tenant, pk=ref_id, send_at__isnull=False
        ).delete()
        return Response({"message": "Reminder deleted successfully"},
                        status=200 if deleted else 404)


class ReminderMonthView(APIView):
    def get(self, request, month):
        tenant = tenant_for(request)
        try:
            year, month_no = [int(v) for v in month.split("-")[:2]]
        except (ValueError, AttributeError):
            now = timezone.now()
            year, month_no = now.year, now.month
        qs = Notification.objects.filter(
            tenant=tenant, send_at__year=year, send_at__month=month_no
        ).select_related("lead__company", "lead__client_detail",
                         "deal__company", "deal__client_detail")
        grouped = defaultdict(list)
        for item in qs:
            grouped[str(item.send_at.day)].append({
                "title": item.title,
                "lead_id": item.lead_id,
                "deal_id": item.deal_id,
                "company_name": (
                    item.lead.company.name if item.lead_id else item.deal.company.name
                ),
                "client_name": (
                    item.lead.client_detail.first_name
                    if item.lead_id else item.deal.client_detail.first_name
                ),
            })
        return Response({"message": "Reminders fetched successfully",
                         "remindersByDay": grouped, "grouped": {}})


class NotificationListView(APIView):
    read = False

    def get(self, request):
        tenant = tenant_for(request)
        recipients = NotificationRecipient.objects.filter(
            tenant=tenant, user=request.user, is_read=self.read
        ).select_related("notification").order_by("-notification__created_at")
        return Response({
            "message": "Notifications fetched successfully",
            "notifications": NotificationRecipientSerializer(recipients, many=True).data,
        })


class ReadNotificationListView(NotificationListView):
    read = True


class MarkNotificationView(APIView):
    def post(self, request, notification_id=None):
        tenant = tenant_for(request)
        qs = NotificationRecipient.objects.filter(tenant=tenant, user=request.user)
        if notification_id is not None:
            qs = qs.filter(pk=notification_id)
        count = qs.update(is_read=True, read_at=timezone.now())
        return Response({
            "message": (
                "All notifications marked read" if notification_id is None
                else "Notification marked read"
            ),
            "updated": count,
        })


def create_quotation_graph(quotation, products, tenant):
    for product_data in products or []:
        product = QuotationProduct.objects.create(
            tenant=tenant, quotation=quotation, name=product_data["name"]
        )
        for item in product_data.get("items", []):
            if item.get("removed"):
                continue
            QuotationItem.objects.create(
                tenant=tenant, quotation_product=product,
                item_name=item["name"], item_code=item.get("code"),
                description=item.get("description"), height=item.get("height", 0),
                width=item.get("width", 0), depth=item.get("depth", 0),
                quantity=item.get("quantity", 0), per_bay_qty=item.get("per_bay_qty", 0),
                provided_rate=item.get("provided_rate", 0),
                market_rate=item.get("market_rate", 0),
            )
        QuotationWorking.objects.create(
            tenant=tenant, quotation_product=product,
            total_weight=product_data.get("total_weight", 0),
            ss_material=product_data.get("ss_material", 0),
            trolley_material=product_data.get("trolley_material", 0),
            powder_coating=product_data.get("powder_coating", 0),
            labour_cost=product_data.get("labour_cost", 0),
            installation=product_data.get("installation", 0),
            transport=product_data.get("transport", 0),
            accomodation=product_data.get("accomodation", 0),
            provided_total_cost=product_data.get("total_provided_rate", 0),
            market_total_cost=product_data.get("total_market_rate", 0),
            total_body=product_data.get("total_body", 0),
            metal_rate=product_data.get("metal_rate", ""),
            set=product_data.get("set", 1),
            profit_percent=product_data.get("profit_percent", 0),
            discount=product_data.get("discount", 0),
        )


class QuotationProductsView(APIView):
    def post(self, request):
        tenant = tenant_for(request)
        qs = BaseProduct.objects.filter(tenant=tenant)
        if request.data.get("product_type"):
            qs = qs.filter(product_type=request.data["product_type"])
        return Response({
            "message": "Products fetched successfully",
            "products": BaseProductSerializer(qs, many=True).data,
        })


class QuotationListCreateView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        qs = Quotation.objects.filter(tenant=tenant).select_related(
            "deal", "created_by"
        ).prefetch_related(
            "quotation_products__quotation_item",
            "quotation_products__quotation_working",
        )
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(quotation_no__icontains=search) | Q(deal_id__icontains=search)
            )
        qs = qs.filter(date_filters(request)).order_by("-created_at")
        total = qs.count()
        return Response({
            "message": "Quotations fetched successfully",
            "convertedQuotation": QuotationSerializer(
                paginate(qs, request), many=True
            ).data,
            "totalQuotations": total,
        })

    @transaction.atomic
    def post(self, request, deal_id):
        tenant = tenant_for(request)
        deal = Deal.objects.filter(tenant=tenant, pk=deal_id).first()
        if not deal:
            return Response({"message": "Deal not found"}, status=404)
        quotation = Quotation.objects.create(
            tenant=tenant, deal=deal, created_by=request.user,
            quotation_no=request.data["quotation_no"],
            quotation_template=request.data["quotation_template"],
            gst=request.data.get("gst", 0), round_off=request.data.get("round_off", 0),
            sub_total=request.data.get("total", 0),
            grand_total=request.data.get("grandTotal", 0),
            show_body_table=request.data.get("show_body_table", True),
            note=request.data.get("note"),
            specifications=request.data.get("specifications"),
            terms_and_condition=request.data.get("terms_and_condition"),
        )
        create_quotation_graph(
            quotation, request.data.get("quotation_item", []), tenant
        )
        deal.deal_status = Deal.Status.QUOTATION
        deal.save(update_fields=["deal_status"])
        return Response({"message": "Quotation added successfully", "id": quotation.pk})


class QuotationByDealView(APIView):
    def get(self, request, deal_id):
        tenant = tenant_for(request)
        qs = Quotation.objects.filter(tenant=tenant, deal_id=deal_id)
        return Response({
            "message": "Quotations fetched successfully",
            "quotations": list(qs.values(
                "id", "deal_id", "created_at", "grand_total", "quotation_no"
            )),
        })


class QuotationDetailView(APIView):
    def get(self, request, quotation_id):
        tenant = tenant_for(request)
        quotation = Quotation.objects.filter(
            tenant=tenant, pk=quotation_id
        ).prefetch_related(
            "quotation_products__quotation_item",
            "quotation_products__quotation_working",
        ).first()
        return Response({
            "message": "Quotation fetched successfully",
            "quotation": QuotationSerializer(quotation).data if quotation else None,
        }, status=200 if quotation else 404)

    @transaction.atomic
    def put(self, request, quotation_id, deal_id=None):
        tenant = tenant_for(request)
        quotation = Quotation.objects.select_for_update().filter(
            tenant=tenant, pk=quotation_id, deal_id=deal_id
        ).first()
        if not quotation:
            return Response({"message": "Quotation not found"}, status=404)
        mapping = {
            "quotation_template": "quotation_template", "quotation_no": "quotation_no",
            "gst": "gst", "round_off": "round_off", "total": "sub_total",
            "grandTotal": "grand_total", "show_body_table": "show_body_table",
            "note": "note", "specifications": "specifications",
            "terms_and_condition": "terms_and_condition",
        }
        for source, target in mapping.items():
            if source in request.data:
                setattr(quotation, target, request.data[source])
        quotation.save()
        if "quotation_item" in request.data:
            quotation.quotation_products.all().delete()
            create_quotation_graph(quotation, request.data["quotation_item"], tenant)
        return Response({"message": "Quotation edited successfully"})

    def delete(self, request, quotation_id):
        tenant = tenant_for(request)
        deleted, _ = Quotation.objects.filter(
            tenant=tenant, pk=quotation_id
        ).delete()
        return Response({"message": "Quotation deleted successfully"},
                        status=200 if deleted else 404)


class QuotationImportView(APIView):
    @transaction.atomic
    def post(self, request, quotation_id):
        tenant = tenant_for(request)
        source = Quotation.objects.filter(tenant=tenant, pk=quotation_id).prefetch_related(
            "quotation_products__quotation_item",
            "quotation_products__quotation_working",
        ).first()
        deal = Deal.objects.filter(tenant=tenant, pk=request.data.get("deal_id")).first()
        if not source or not deal:
            return Response({"message": "Source quotation or deal not found"}, status=404)
        clone = Quotation.objects.create(
            tenant=tenant, deal=deal, created_by=request.user,
            quotation_no=request.data["quotation_no"],
            quotation_template=source.quotation_template, gst=source.gst,
            round_off=source.round_off, sub_total=source.sub_total,
            grand_total=source.grand_total, show_body_table=source.show_body_table,
            note=source.note, specifications=source.specifications,
            terms_and_condition=source.terms_and_condition,
        )
        for source_product in source.quotation_products.all():
            product = QuotationProduct.objects.create(
                tenant=tenant, quotation=clone, name=source_product.name
            )
            for item in source_product.quotation_item.all():
                item.pk = None
                item.tenant = tenant
                item.quotation_product = product
                item.save()
            for working in source_product.quotation_working.all():
                working.pk = None
                working.tenant = tenant
                working.quotation_product = product
                working.save()
        return Response({"message": "Quotation imported successfully", "id": clone.pk})


class CompactorView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        return Response({
            "message": "Compactors fetched successfully",
            "compactors": list(BaseProduct.objects.filter(tenant=tenant).values(
                "name", "code", "product_type"
            ).distinct()),
        })


class QuotationNumberView(APIView):
    def get(self, request, quotation_no):
        tenant = tenant_for(request)
        quotation = Quotation.objects.filter(
            tenant=tenant, quotation_no=quotation_no
        ).prefetch_related("quotation_products__quotation_working").first()
        if not quotation:
            return Response({"message": "Quotation not found"}, status=404)
        working = QuotationWorking.objects.filter(
            tenant=tenant, quotation_product__quotation=quotation
        ).first()
        return Response({
            "message": "Quotation fetched successfully",
            "quotation": {
                "grand_total": quotation.grand_total,
                "total_body": working.total_body if working else 0,
                "height": "",
            },
        })


class OrderListCreateView(APIView):
    def get(self, request):
        tenant = tenant_for(request)
        qs = Order.objects.filter(tenant=tenant).select_related(
            "deal", "quotation"
        ).prefetch_related("advance", "colour_change__user")
        search = request.query_params.get("search")
        if search:
            query = Q(deal_id__icontains=search)
            if search.isdigit():
                query |= Q(order_number=int(search))
            qs = qs.filter(query)
        qs = qs.filter(date_filters(request)).order_by("-created_at")
        total = qs.count()
        return Response({
            "message": "Orders fetched successfully",
            "orders": OrderSerializer(paginate(qs, request), many=True).data,
            "totalOrders": total,
        })

    @transaction.atomic
    def post(self, request):
        tenant = tenant_for(request)
        deal = Deal.objects.filter(tenant=tenant, pk=request.data["deal_id"]).first()
        quotation = Quotation.objects.filter(
            tenant=tenant, quotation_no=request.data["quotation_no"], deal=deal
        ).first()
        if not deal or not quotation:
            return Response({"message": "Deal or quotation not found"}, status=404)
        order_number = (
            (Order.objects.filter(tenant=tenant).order_by("-order_number")
             .values_list("order_number", flat=True).first() or 0) + 1
        )
        order = Order.objects.create(
            tenant=tenant, order_number=order_number, deal=deal, quotation=quotation,
            dispatch_at=request.data["dispatch_at"], status=request.data["status"],
            po_number=request.data.get("po_number"),
            pi_number=request.data.get("pi_number", False),
            bill_number=request.data.get("bill_number"),
            fitted_by=request.data.get("fitted_by"),
            powder_coating=request.data.get("powder_coating", False),
            count_order=request.data.get("count_order", False),
            balance=request.data.get("total", 0), height=request.data["height"],
            total_body=request.data["total_body"],
        )
        deal.deal_status = Deal.Status.ORDER_CONFIRMED
        deal.save(update_fields=["deal_status"])
        return Response({"message": "Order added successfully", "id": order.pk})


class OrderDetailView(APIView):
    def get(self, request, order_id):
        tenant = tenant_for(request)
        order = Order.objects.filter(tenant=tenant, pk=order_id).select_related(
            "deal", "quotation"
        ).prefetch_related("advance", "colour_change__user").first()
        return Response({
            "message": "Order fetched successfully",
            "order": OrderSerializer(order).data if order else None,
        }, status=200 if order else 404)

    def put(self, request, order_id):
        tenant = tenant_for(request)
        order = Order.objects.filter(tenant=tenant, pk=order_id).first()
        if not order:
            return Response({"message": "Order not found"}, status=404)
        fields = (
            "dispatch_at", "status", "po_number", "pi_number", "bill_number",
            "fitted_by", "powder_coating", "count_order", "height", "total_body",
        )
        for field in fields:
            if field in request.data:
                setattr(order, field, request.data[field])
        order.save()
        return Response({"message": "Order edited successfully"})

    def delete(self, request, order_id):
        tenant = tenant_for(request)
        deleted, _ = Order.objects.filter(tenant=tenant, pk=order_id).delete()
        return Response({"message": "Order deleted successfully"},
                        status=200 if deleted else 404)


class OrderColourView(APIView):
    def post(self, request, order_id):
        tenant = tenant_for(request)
        order = Order.objects.filter(tenant=tenant, pk=order_id).first()
        if not order:
            return Response({"message": "Order not found"}, status=404)
        ColourChange.objects.create(
            tenant=tenant, order=order, user=request.user,
            colour=request.data["colour"],
        )
        create_assignment_notification(
            tenant, "color_changed", "Order colour changed",
            f"Order {order.order_number} colour changed to {request.data['colour']}",
            order.deal.assigned_to.all(), order=order,
        )
        return Response({"message": "Colour added successfully"})


class OrderPaymentView(APIView):
    def post(self, request, order_id):
        tenant = tenant_for(request)
        order = Order.objects.filter(tenant=tenant, pk=order_id).first()
        if not order:
            return Response({"message": "Order not found"}, status=404)
        amount = int(request.data["amount"])
        Advance.objects.create(
            tenant=tenant, order=order, advance_amount=amount,
            advance_date=request.data["date"],
        )
        order.balance -= amount
        order.save(update_fields=["balance"])
        return Response({"message": "Payment added successfully"})

    def put(self, request, payment_id):
        tenant = tenant_for(request)
        advance = Advance.objects.select_related("order").filter(
            tenant=tenant, pk=payment_id
        ).first()
        if not advance:
            return Response({"message": "Payment not found"}, status=404)
        old = advance.advance_amount
        advance.advance_amount = int(request.data["amount"])
        advance.advance_date = request.data["date"]
        advance.save()
        advance.order.balance += old - advance.advance_amount
        advance.order.save(update_fields=["balance"])
        return Response({"message": "Payment edited successfully"})

    def delete(self, request, payment_id):
        tenant = tenant_for(request)
        advance = Advance.objects.select_related("order").filter(
            tenant=tenant, pk=payment_id
        ).first()
        if not advance:
            return Response({"message": "Payment not found"}, status=404)
        order, amount = advance.order, advance.advance_amount
        advance.delete()
        order.balance += amount
        order.save(update_fields=["balance"])
        return Response({"message": "Payment deleted successfully"})


def s3_client(public=False):
    return boto3.client(
        "s3",
        endpoint_url=(
            settings.AWS_S3_PUBLIC_ENDPOINT_URL
            if public else settings.AWS_S3_ENDPOINT_URL
        ),
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name="auto",
    )


class DrawingUploadUrlView(APIView):
    def post(self, request):
        tenant = tenant_for(request)
        file_name = request.data.get("fileName")
        file_type = request.data.get("fileType")
        upload_type = request.data.get("upload_type")
        if not file_name or not file_type or upload_type not in Drawing.UploadType.values:
            return Response({"message": "Input validation error"}, status=400)
        key = f"{tenant.pk}/{upload_type}/{uuid4().hex}-{file_name}"
        url = s3_client(public=True).generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.AWS_STORAGE_BUCKET_NAME,
                "Key": key,
                "ContentType": file_type,
            },
            ExpiresIn=300,
        )
        return Response({
            "message": "Upload url fetched successfully",
            "uploadUrl": url, "fileKey": key,
        })


class DrawingCreateView(APIView):
    def post(self, request):
        tenant = tenant_for(request)
        context = request.data.get("context")
        deal = None
        order = None
        if context == "deal":
            deal = Deal.objects.filter(
                tenant=tenant, pk=request.data.get("deal_id")
            ).first()
            if not deal:
                return Response({"message": "Deal not found"}, status=404)
        elif context == "order":
            order = Order.objects.filter(
                tenant=tenant, pk=request.data.get("order_id")
            ).first()
            if not order:
                return Response({"message": "Order not found"}, status=404)
        else:
            return Response({"message": "Invalid drawing context"}, status=400)
        drawing = Drawing.objects.create(
            tenant=tenant, file_url=request.data["drawing_url"],
            title=request.data["title"], version=request.data.get("version"),
            upload_type=request.data["upload_type"],
            file_size=request.data["file_size"], file_type=request.data["file_type"],
            uploaded_by=request.user,
            deal=deal, order=order,
            status=(
                Drawing.Status.APPROVED
                if request.data["upload_type"] == Drawing.UploadType.GENERAL
                or request.user.department == User.Department.ADMIN
                else Drawing.Status.PENDING
            ),
        )
        return Response({"message": "Drawing uploaded successfully", "id": drawing.pk})


class DrawingListView(APIView):
    def get(self, request, ref_id=None):
        tenant = tenant_for(request)
        qs = Drawing.objects.filter(tenant=tenant).select_related("uploaded_by")
        if ref_id is not None:
            context = request.query_params.get("context", "deal")
            qs = qs.filter(order_id=ref_id) if context == "order" else qs.filter(deal_id=ref_id)
            data = {
                state: DrawingSerializer(qs.filter(status=state), many=True).data
                for state in Drawing.Status.values
            }
            return Response({
                "message": "Drawing fetched successfully",
                "drawings": data, "totalDrawing": qs.count(),
            })
        search = request.query_params.get("search")
        if search:
            query = Q(title__icontains=search) | Q(deal_id__icontains=search)
            if search.isdigit():
                query |= Q(order_id=int(search))
            qs = qs.filter(query)
        qs = qs.filter(status=Drawing.Status.APPROVED).order_by("-created_at")
        total = qs.count()
        return Response({
            "message": "All drawings fetched successfully",
            "drawings": DrawingSerializer(paginate(qs, request), many=True).data,
            "totalDrawing": total,
        })

    def post(self, request, ref_id=None):
        tenant = tenant_for(request)
        drawing = Drawing.objects.filter(tenant=tenant, pk=ref_id).first()
        if not drawing:
            return Response({"message": "Drawing not found"}, status=404)
        url = s3_client(public=True).generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": drawing.file_url},
            ExpiresIn=300,
        )
        return Response({"message": "View URL fetched successfully", "viewUrl": url})


class DrawingDetailView(APIView):
    def post(self, request, drawing_id):
        tenant = tenant_for(request)
        drawing = Drawing.objects.filter(tenant=tenant, pk=drawing_id).first()
        if not drawing:
            return Response({"message": "Drawing not found"}, status=404)
        url = s3_client(public=True).generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": drawing.file_url},
            ExpiresIn=300,
        )
        return Response({"message": "View URL fetched successfully", "viewUrl": url})

    def delete(self, request, drawing_id):
        tenant = tenant_for(request)
        drawing = Drawing.objects.filter(tenant=tenant, pk=drawing_id).first()
        if not drawing:
            return Response({"message": "Drawing not found"}, status=404)
        s3_client().delete_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=drawing.file_url
        )
        drawing.delete()
        return Response({"message": "Drawing deleted successfully"})


class DrawingStatusView(APIView):
    status_value = None

    def post(self, request, drawing_id):
        tenant = tenant_for(request)
        drawing = Drawing.objects.filter(tenant=tenant, pk=drawing_id).first()
        if not drawing:
            return Response({"message": "Drawing not found"}, status=404)
        drawing.status = self.status_value
        drawing.note = request.data.get("note")
        drawing.approved_at = (
            timezone.now() if self.status_value == Drawing.Status.APPROVED else None
        )
        drawing.save()
        return Response({"message": f"Drawing {self.status_value} successfully"})


class ApproveDrawingView(DrawingStatusView):
    status_value = Drawing.Status.APPROVED


class RejectDrawingView(DrawingStatusView):
    status_value = Drawing.Status.REJECTED


class DrawingShowInOrderView(APIView):
    def patch(self, request, drawing_id):
        tenant = tenant_for(request)
        drawing = Drawing.objects.filter(tenant=tenant, pk=drawing_id).first()
        if not drawing:
            return Response({"message": "Drawing not found"}, status=404)
        drawing.show_in_order = not drawing.show_in_order
        drawing.order = (
            getattr(drawing.deal, "order", None) if drawing.show_in_order else None
        )
        drawing.save(update_fields=["show_in_order", "order"])
        return Response({"message": "Drawing order visibility updated"})
