from django.db import transaction
from rest_framework import serializers

from .models import (
    Advance,
    AuditLog,
    BaseProduct,
    Branch,
    BusinessPermission,
    Client,
    ClientEmail,
    ClientPhone,
    ColourChange,
    Company,
    Deal,
    Description,
    Drawing,
    Invoice,
    Lead,
    Notification,
    NotificationRecipient,
    Order,
    Product,
    Quotation,
    QuotationItem,
    QuotationProduct,
    QuotationWorking,
    Role,
    RolePermission,
    Source,
    SubscriptionPlan,
    Tenant,
    TenantMembership,
    TenantSettings,
    TenantSubscription,
    User,
    UserRole,
)


class UserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    code = serializers.CharField(source="quotation_code", read_only=True)

    class Meta:
        model = User
        fields = (
            "id", "first_name", "last_name", "name", "email", "phone",
            "department", "quotation_code", "code", "platform_admin",
        )
        read_only_fields = ("platform_admin",)

    def get_name(self, obj):
        return obj.get_full_name()


class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(min_length=6, write_only=True)

    class Meta:
        model = User
        fields = (
            "first_name", "last_name", "email", "phone", "password",
            "department", "quotation_code",
        )

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = "__all__"


class TenantSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantSettings
        exclude = ("tenant",)


class TenantSerializer(serializers.ModelSerializer):
    settings = TenantSettingsSerializer(read_only=True)
    plan = SubscriptionPlanSerializer(read_only=True)

    class Meta:
        model = Tenant
        fields = "__all__"


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        exclude = ("tenant",)


class BusinessPermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessPermission
        fields = "__all__"


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = (
            "id", "tenant", "name", "code", "description", "is_system",
            "approved_for_tenant_assignment", "is_active", "permissions",
        )
        read_only_fields = ("tenant", "is_system")

    def get_permissions(self, obj):
        return list(obj.permission_links.values_list("permission__code", flat=True))


class RolePermissionSerializer(serializers.ModelSerializer):
    permission_code = serializers.CharField(source="permission.code", read_only=True)

    class Meta:
        model = RolePermission
        fields = ("id", "role", "permission", "permission_code")


class UserRoleSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source="role.name", read_only=True)
    permission_codes = serializers.SerializerMethodField()

    class Meta:
        model = UserRole
        fields = (
            "id", "user", "role", "role_name", "branch", "valid_from", "valid_to",
            "assigned_by", "is_active", "permission_codes",
        )
        read_only_fields = ("assigned_by",)

    def get_permission_codes(self, obj):
        return list(
            obj.role.permission_links.values_list("permission__code", flat=True)
        )


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True)

    class Meta:
        model = AuditLog
        fields = "__all__"


class ClientEmailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientEmail
        fields = ("email",)


class ClientPhoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClientPhone
        fields = ("phone",)


class ClientSerializer(serializers.ModelSerializer):
    emails = ClientEmailSerializer(many=True, read_only=True)
    phones = ClientPhoneSerializer(many=True, read_only=True)

    class Meta:
        model = Client
        fields = ("id", "company_id", "first_name", "last_name", "emails", "phones")


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        exclude = ("tenant", "updated_at")


class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = ("id", "name")


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name")


class AssigneeSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ("user",)

    def get_user(self, obj):
        return {
            "id": obj.id,
            "first_name": obj.first_name,
            "last_name": obj.last_name,
            "quotation_code": obj.quotation_code,
            "phone": obj.phone,
        }


class LeadSerializer(serializers.ModelSerializer):
    company = CompanySerializer(read_only=True)
    client_detail = ClientSerializer(read_only=True)
    source = SourceSerializer(read_only=True)
    product = ProductSerializer(read_only=True)
    assigned_to = AssigneeSerializer(many=True, read_only=True)

    class Meta:
        model = Lead
        fields = "__all__"


class DealSerializer(serializers.ModelSerializer):
    company = CompanySerializer(read_only=True)
    client_detail = ClientSerializer(read_only=True)
    source = SourceSerializer(read_only=True)
    product = ProductSerializer(read_only=True)
    assigned_to = AssigneeSerializer(many=True, read_only=True)

    class Meta:
        model = Deal
        fields = "__all__"


class DescriptionSerializer(serializers.ModelSerializer):
    user = AssigneeSerializer(source="updated_by", read_only=True)

    class Meta:
        model = Description
        exclude = ("tenant",)


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        exclude = ("tenant", "updated_at")


class NotificationRecipientSerializer(serializers.ModelSerializer):
    notification = NotificationSerializer(read_only=True)

    class Meta:
        model = NotificationRecipient
        exclude = ("tenant", "updated_at")


class BaseProductSerializer(serializers.ModelSerializer):
    height = serializers.IntegerField(source="default_height")
    width = serializers.IntegerField(source="default_width")
    depth = serializers.IntegerField(source="default_depth")
    quantity = serializers.SerializerMethodField()
    provided_rate = serializers.SerializerMethodField()
    market_rate = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    removed = serializers.SerializerMethodField()

    class Meta:
        model = BaseProduct
        exclude = ("tenant", "created_at", "updated_at")

    def get_quantity(self, obj): return 1
    def get_provided_rate(self, obj): return 0
    def get_market_rate(self, obj): return 0
    def get_description(self, obj): return None
    def get_removed(self, obj): return False


class QuotationItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuotationItem
        exclude = ("tenant", "created_at", "updated_at")


class QuotationWorkingSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuotationWorking
        exclude = ("tenant", "created_at", "updated_at")


class QuotationProductSerializer(serializers.ModelSerializer):
    quotation_item = QuotationItemSerializer(many=True, read_only=True)
    quotation_working = QuotationWorkingSerializer(many=True, read_only=True)

    class Meta:
        model = QuotationProduct
        exclude = ("tenant", "created_at", "updated_at", "quotation")


class QuotationSerializer(serializers.ModelSerializer):
    quotation_products = QuotationProductSerializer(many=True, read_only=True)
    deal = DealSerializer(read_only=True)

    class Meta:
        model = Quotation
        fields = "__all__"


class AdvanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Advance
        exclude = ("tenant", "created_at", "updated_at")


class ColourChangeSerializer(serializers.ModelSerializer):
    user = AssigneeSerializer(read_only=True)

    class Meta:
        model = ColourChange
        exclude = ("tenant", "updated_at")


class OrderSerializer(serializers.ModelSerializer):
    advance = AdvanceSerializer(many=True, read_only=True)
    colour_change = ColourChangeSerializer(many=True, read_only=True)
    quotation_no = serializers.CharField(source="quotation.quotation_no", read_only=True)
    deal_id = serializers.CharField(source="deal.id", read_only=True)
    deal = DealSerializer(read_only=True)
    quotation = QuotationSerializer(read_only=True)

    class Meta:
        model = Order
        fields = "__all__"


class DrawingSerializer(serializers.ModelSerializer):
    user = AssigneeSerializer(source="uploaded_by", read_only=True)

    class Meta:
        model = Drawing
        fields = "__all__"


class TenantSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantSubscription
        fields = "__all__"
        read_only_fields = ("tenant",)


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = "__all__"
