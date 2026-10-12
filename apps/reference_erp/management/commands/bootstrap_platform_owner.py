from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.conf import settings
from apps.core.models import User
from apps.erp.otp import normalize_phone
import uuid


class Command(BaseCommand):
    help="Provision the explicitly designated platform owner by registered mobile number."

    def add_arguments(self,parser):
        parser.add_argument("--phone",required=True)
        parser.add_argument("--name",default="Platform Owner")

    def handle(self,*args,**options):
        if not settings.DEBUG and (settings.ERP_OTP_MODE!="sms" or not settings.ERP_SMS_ENABLED):
            raise CommandError("Activate MSG91 SMS login before provisioning the production platform owner. Workflows can be tested in the isolated development profile.")
        phone=normalize_phone(options["phone"])
        user=User.objects.filter(Q(phone_e164=phone)|Q(phone=phone[-10:])).first()
        if user and not user.is_active:raise CommandError("The designated identity is inactive.")
        if not user:
            first,_,last=options["name"].partition(" ")
            user=User.objects.create_user(email=f"{phone[1:]}.{uuid.uuid4().hex[:8]}@phone.platform.invalid",phone=phone,password=None,
                                          first_name=first,last_name=last,department="admin")
        user.platform_admin=True;user.save(update_fields=["platform_admin"])
        self.stdout.write(self.style.SUCCESS(f"Platform dashboard access provisioned for user {user.pk}. Sign in with phone OTP."))
