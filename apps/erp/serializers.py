from django.core.exceptions import ValidationError as ModelValidationError
from rest_framework import serializers

from apps.core.models import Company, TenantMembership, Branch
from . import models as m
from .money import calculate_line
from .services import number, total_document


class ScopedSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and getattr(request, "tenant", None):
            for field in self.fields.values():
                qs = getattr(field, "queryset", None)
                if qs is None: continue
                names = {f.name for f in qs.model._meta.fields}
                if "tenant" in names:
                    field.queryset = qs.filter(tenant=request.tenant)
                elif qs.model._meta.label == "core.User":
                    field.queryset = qs.filter(tenant_memberships__tenant=request.tenant, tenant_memberships__is_active=True, is_active=True)
                if "archived" in names: field.queryset = field.queryset.filter(archived=False)

    def get_label(self, obj):
        return str(getattr(obj, "name", None) or getattr(obj, "title", None) or getattr(obj, "number", None) or getattr(obj, "code", None) or getattr(obj, "description", None) or obj.pk)

    def validate(self, attrs):
        request = self.context.get("request")
        model = self.Meta.model
        obj = self.instance or model()
        for key, value in attrs.items():
            if not isinstance(value, list): setattr(obj, key, value)
        if request:
            obj.tenant = request.tenant
        if "custom_fields" in attrs:
            from .configuration import validate_custom_values
            validate_custom_values(obj, attrs["custom_fields"])
        try:
            obj.clean()
        except ModelValidationError as exc:
            raise serializers.ValidationError(getattr(exc, "message_dict", exc.messages))
        return attrs

    def to_representation(self, obj):
        result = super().to_representation(obj)
        request = self.context.get("request")
        if request and isinstance(obj, (m.StockBalance, m.StockMovement)):
            from .security import has_permission
            if not has_permission(request, "stock.view_cost"):
                for key in ("value", "unit_cost"):
                    result.pop(key, None)
        for field in obj._meta.fields:
            if field.is_relation and field.name not in ("tenant", "created_by") and getattr(obj, field.attname, None):
                related = getattr(obj, field.name)
                result[field.name + "_label"] = str(getattr(related, "name", None) or getattr(related, "number", None) or getattr(related, "title", None) or getattr(related, "code", None) or getattr(related, "email", None) or related.pk)
        return result


READ_ONLY = ("id", "created_by", "created_at", "updated_at", "version", "archived")


def model_serializer(model, readonly=()):
    return type(model.__name__ + "Serializer", (ScopedSerializer,), {
        "Meta": type("Meta", (), {"model": model, "exclude": ("tenant",), "read_only_fields": READ_ONLY + tuple(readonly)})})


ItemSerializer = model_serializer(m.Item)
SupplierSerializer = model_serializer(m.Supplier)
WarehouseSerializer = model_serializer(m.Warehouse)
BinSerializer = model_serializer(m.WarehouseBin)
DepartmentSerializer = model_serializer(m.Department)
CostCenterSerializer = model_serializer(m.CostCenter)
JobStageSerializer = model_serializer(m.JobStage, ("status", "completed", "rejected", "rework"))


class JobSerializer(ScopedSerializer):
    stages = JobStageSerializer(many=True, read_only=True)
    class Meta:
        model = m.Job
        exclude = ("tenant",)
        read_only_fields = READ_ONLY + ("number", "status", "completed_quantity", "completed_at")


class EmployeeSerializer(ScopedSerializer):
    class Meta:
        model = m.Employee
        exclude = ("tenant",)
        read_only_fields = READ_ONLY

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "tenant", None):
            from .security import has_permission
            if not has_permission(request, "employee.view_private"):
                for key in ("monthly_salary", "notes", "phone", "email"):
                    fields.pop(key, None)
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        obj = self.instance
        joining = attrs.get("joining_date", getattr(obj, "joining_date", None))
        exit_date = attrs.get("exit_date", getattr(obj, "exit_date", None))
        if exit_date and joining and exit_date < joining:
            raise serializers.ValidationError({"exit_date": "Exit cannot precede joining."})
        manager = attrs.get("manager")
        visited = {obj.pk} if obj else set()
        while manager:
            if manager.pk in visited: raise serializers.ValidationError({"manager": "Manager relationships cannot form a cycle."})
            visited.add(manager.pk); manager = manager.manager
        return attrs


AttendanceSerializer = model_serializer(m.Attendance, ("locked",))
ShiftSerializer = model_serializer(m.Shift)
HolidaySerializer = model_serializer(m.Holiday)
LeaveTypeSerializer = model_serializer(m.LeaveType)
LeaveSerializer = model_serializer(m.LeaveRequest, ("days", "status", "reviewed_by"))
SalaryComponentSerializer = model_serializer(m.SalaryComponent)
LoanSerializer = model_serializer(m.EmployeeLoan, ("recovered", "status"))
PayrollResultSerializer = model_serializer(m.PayrollResult)


class PayrollSerializer(ScopedSerializer):
    results = PayrollResultSerializer(many=True, read_only=True)
    class Meta:
        model = m.PayrollRun
        exclude = ("tenant",)
        read_only_fields = READ_ONLY + ("status", "gross", "deductions", "net", "employer_cost", "input_hash", "finalized_at")

    def validate_month(self, value):
        return value.replace(day=1)


class LineSerializer(ScopedSerializer):
    def to_representation(self, obj):
        result = super().to_representation(obj)
        from .services import source_remaining
        result["remaining"] = {kind: str(source_remaining(obj, kind)) for kind in ("sales_order", "goods_receipt", "dispatch", "invoice", "supplier_bill", "credit_note", "supplier_return")}
        return result

    class Meta:
        model = m.DocumentLine
        exclude = ("tenant", "document", "created_by", "custom_fields", "archived", "branch")
        read_only_fields = ("id", "source_line", "order_line", "taxable", "tax", "gross", "cgst", "sgst", "igst", "created_at", "updated_at", "version")


class DocumentSerializer(ScopedSerializer):
    lines = LineSerializer(many=True)
    class Meta:
        model = m.Document
        exclude = ("tenant",)
        read_only_fields = READ_ONLY + ("number", "status", "source", "crm_order", "crm_quotation", "taxable", "tax", "gross", "paid", "snapshot", "revision", "posted_at", "delivery_status")

    def validate(self, attrs):
        lines = attrs.pop("lines", None)
        attrs = super().validate(attrs)
        if lines is not None:
            if not 1 <= len(lines) <= 500:
                raise serializers.ValidationError({"lines": "Provide between 1 and 500 lines."})
            for i, row in enumerate(lines):
                item = row.get("item")
                if item and item.tenant_id != self.context["request"].tenant.pk:
                    raise serializers.ValidationError({"lines": f"Line {i+1} uses an item from another company."})
            attrs["lines"] = lines
        return attrs

    def save_lines(self, doc, lines):
        for i, row in enumerate(lines):
            calculated = calculate_line(row, doc.tax_mode, doc.tax_jurisdiction)
            m.DocumentLine.objects.create(tenant=doc.tenant, branch=doc.branch, document=doc,
                item=row.get("item"), description=row["description"], unit=row.get("unit", "pcs"), position=i,
                accepted=row.get("accepted", 0), rejected=row.get("rejected", 0), damaged=row.get("damaged", 0), **calculated)
        total_document(doc)

    def create(self, validated_data):
        lines = validated_data.pop("lines")
        doc = m.Document.objects.create(number=number(validated_data["tenant"], validated_data["kind"]), **validated_data)
        self.save_lines(doc, lines)
        return doc

    def update(self, instance, validated_data):
        if "kind" in validated_data and validated_data["kind"] != instance.kind:
            raise serializers.ValidationError({"kind": "Document type cannot be changed."})
        lines = validated_data.pop("lines", None)
        if instance.source_id:
            for key in ("customer", "supplier", "tax_mode", "tax_jurisdiction"):
                if key in validated_data and validated_data[key] != getattr(instance, key):
                    raise serializers.ValidationError({key: "Source commercial terms cannot be changed during conversion."})
        if instance.source_id and lines is not None:
            raise serializers.ValidationError({"lines": "Converted prices and source allocations are fixed. Use receipt quantities or a commercial amendment."})
        instance = super().update(instance, validated_data)
        if lines is not None:
            instance.lines.all().delete()
            self.save_lines(instance, lines)
        return instance


CategorySerializer = model_serializer(m.ExpenseCategory)
ExpenseSerializer = model_serializer(m.Expense, ("status", "recurring_template"))
RecurringSerializer = model_serializer(m.RecurringExpense)
class TaskCommentSerializer(ScopedSerializer):
    class Meta:
        model = m.TaskComment
        exclude = ("tenant", "task", "custom_fields", "archived", "branch")
        read_only_fields = READ_ONLY + ("content",)
    def to_representation(self,obj):
        result=super().to_representation(obj);result["author"]=obj.created_by.get_full_name() or obj.created_by.email;return result


class TaskSerializer(ScopedSerializer):
    comments = TaskCommentSerializer(many=True, read_only=True)
    class Meta:
        model = m.Task
        exclude = ("tenant",)
        read_only_fields = READ_ONLY
    def validate_checklist(self,value):
        if not isinstance(value,list) or len(value)>50:raise serializers.ValidationError("Checklist must contain up to 50 items.")
        for item in value:
            if not isinstance(item,dict) or set(item)!={"id","text","done"} or not isinstance(item["text"],str) or len(item["text"])>300 or not isinstance(item["done"],bool):
                raise serializers.ValidationError("Each checklist item needs id, text and done fields.")
        return value
ApprovalSerializer = model_serializer(m.Approval)
ApprovalRuleSerializer = model_serializer(m.ApprovalRule)
ConfigSerializer = model_serializer(m.Configuration, ("status",))
StockSerializer = model_serializer(m.StockBalance, ("on_hand", "reserved", "value"))
MovementSerializer = model_serializer(m.StockMovement)
ReservationSerializer = model_serializer(m.Reservation)
PaymentSerializer = model_serializer(m.Payment)
FactSerializer = model_serializer(m.ManagementFact)
AttachmentSerializer = model_serializer(m.Attachment, ("object_key",))
DataJobSerializer = model_serializer(m.DataJob)
PeriodSerializer = model_serializer(m.PeriodLock)
CommunicationSerializer = model_serializer(m.Communication, ("status", "provider_id", "error", "sent_at"))


class ApiCredentialSerializer(ScopedSerializer):
    class Meta:
        model=m.ApiCredential
        exclude=("tenant","key_hash")
        read_only_fields=READ_ONLY+("user","prefix","permissions","expires_at","revoked_at")


class WebhookSerializer(ScopedSerializer):
    secret=serializers.CharField(write_only=True,min_length=24,required=False)
    class Meta:
        model=m.WebhookEndpoint
        exclude=("tenant","secret_ciphertext")
        read_only_fields=READ_ONLY
    def create(self,data):
        from .crypto import encrypt
        secret=data.pop("secret",None)
        if not secret:raise serializers.ValidationError({"secret":"A signing secret of at least 24 characters is required."})
        data["secret_ciphertext"]=encrypt(secret);return super().create(data)
    def update(self,obj,data):
        from .crypto import encrypt
        secret=data.pop("secret",None)
        if secret:obj.secret_ciphertext=encrypt(secret)
        return super().update(obj,data)


class CustomerSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source="name", read_only=True)
    contact_name = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(required=False, allow_blank=True)
    shipping_address = serializers.CharField(required=False, allow_blank=True)
    payment_terms = serializers.IntegerField(required=False, min_value=0)
    credit_limit = serializers.DecimalField(required=False, max_digits=20, decimal_places=2, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True)
    custom_fields = serializers.JSONField(required=False)
    class Meta:
        model = Company
        fields = ("id", "name", "label", "address", "gst_no", "created_at", "updated_at", "contact_name", "email", "phone", "shipping_address", "payment_terms", "credit_limit", "notes", "custom_fields")
        read_only_fields = ("id", "created_at", "updated_at")

    PROFILE_FIELDS = ("contact_name", "email", "phone", "shipping_address", "payment_terms", "credit_limit", "notes", "custom_fields")

    def create(self, data):
        profile = {key: data.pop(key) for key in self.PROFILE_FIELDS if key in data}
        obj = super().create(data)
        record=m.CustomerProfile(tenant=obj.tenant, customer=obj, **profile)
        from .configuration import validate_custom_values
        validate_custom_values(record,record.custom_fields);record.save()
        return obj

    def update(self, obj, data):
        profile = {key: data.pop(key) for key in self.PROFILE_FIELDS if key in data}
        obj = super().update(obj, data)
        record, _ = m.CustomerProfile.objects.get_or_create(tenant=obj.tenant, customer=obj)
        for key, value in profile.items(): setattr(record, key, value)
        from .configuration import validate_custom_values
        validate_custom_values(record,record.custom_fields)
        record.version += 1; record.save()
        return obj

    def to_representation(self, obj):
        # Virtual profile fields are populated without flattening private credentials.
        profile = m.CustomerProfile.objects.filter(customer=obj, tenant=obj.tenant).first()
        for key in self.PROFILE_FIELDS: setattr(obj, key, getattr(profile, key, "" if key not in ("payment_terms", "credit_limit") else 0))
        return super().to_representation(obj)


class PositionSerializer(ScopedSerializer):
    class Meta:
        model = m.StockPosition
        exclude = ("tenant",)
        read_only_fields = READ_ONLY + ("balance", "bin", "quantity")
    def to_representation(self, obj):
        result = super().to_representation(obj)
        result.update(item=obj.balance.item_id,item_label=obj.balance.item.name,warehouse=obj.balance.warehouse_id,
                      warehouse_label=obj.balance.warehouse.name,bin_label=obj.bin.code,unit=obj.balance.item.unit)
        return result

StockCountLineSerializer = model_serializer(m.StockCountLine)
class StockCountSerializer(ScopedSerializer):
    lines = StockCountLineSerializer(many=True, read_only=True)
    class Meta:
        model = m.StockCount
        exclude = ("tenant",)
        read_only_fields = READ_ONLY + ("status",)


class ReservationSerializer(ScopedSerializer):
    class Meta:
        model = m.Reservation
        exclude = ("tenant",)
        read_only_fields = READ_ONLY + ("balance", "quantity", "consumed", "status")
    def to_representation(self, obj):
        result = super().to_representation(obj)
        result.update(item_label=obj.balance.item.name,warehouse_label=obj.balance.warehouse.name,unit=obj.balance.item.unit,
                      available=str(obj.quantity-obj.consumed),order_label=obj.order.number if obj.order else None)
        return result
