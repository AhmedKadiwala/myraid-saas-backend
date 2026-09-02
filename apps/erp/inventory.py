"""Position-level quantities, stock counts, reservation release and reversals."""
from decimal import Decimal
from django.db.models import Sum
from rest_framework.exceptions import ValidationError
from . import models as m
from .money import decimal, ZERO


def ensure_positions(balance):
    default,_=m.WarehouseBin.objects.get_or_create(tenant=balance.tenant,warehouse=balance.warehouse,code="DEFAULT",defaults={"branch":balance.branch,"rack":"Unassigned","pick_priority":999})
    if not balance.positions.exists():
        m.StockPosition.objects.create(tenant=balance.tenant,branch=balance.branch,balance=balance,bin=default,quantity=balance.on_hand)
    return default


def apply_position_change(balance,movement,old_quantity,bin_id=None):
    # Old balances upgraded before position tracking get an explicit default position.
    default,_=m.WarehouseBin.objects.get_or_create(tenant=balance.tenant,warehouse=balance.warehouse,code="DEFAULT",defaults={"branch":balance.branch,"rack":"Unassigned","pick_priority":999})
    if not balance.positions.exists():m.StockPosition.objects.create(tenant=balance.tenant,branch=balance.branch,balance=balance,bin=default,quantity=old_quantity)
    if movement.quantity>0:
        target=m.WarehouseBin.objects.filter(tenant=balance.tenant,warehouse=balance.warehouse,pk=bin_id,archived=False).first() if bin_id else default
        if not target:raise ValidationError("The receiving bin is not in this warehouse.")
        position,_=m.StockPosition.objects.select_for_update().get_or_create(tenant=balance.tenant,balance=balance,bin=target,defaults={"branch":balance.branch})
        position.quantity+=movement.quantity;position.version+=1;position.save()
        m.PositionMovement.objects.create(tenant=balance.tenant,branch=balance.branch,created_by=movement.created_by,balance=balance,bin=target,movement=movement,quantity=movement.quantity,reason=movement.reason[:250])
    else:
        remaining=-movement.quantity
        positions=balance.positions.select_for_update().filter(quantity__gt=0).select_related("bin").order_by("bin__pick_priority","bin__code")
        if bin_id:positions=positions.filter(bin_id=bin_id)
        for position in positions:
            take=min(remaining,position.quantity)
            position.quantity-=take;position.version+=1;position.save()
            m.PositionMovement.objects.create(tenant=balance.tenant,branch=balance.branch,created_by=movement.created_by,balance=balance,bin=position.bin,movement=movement,quantity=-take,reason=movement.reason[:250])
            remaining-=take
            if remaining==0:break
        if remaining:raise ValidationError("There is not enough stock in the selected bin positions.")
    actual=balance.positions.aggregate(q=Sum("quantity"))["q"] or ZERO
    if actual!=balance.on_hand:raise ValidationError("Bin quantities do not reconcile with the warehouse balance.")


def release_reservation(reservation,actor):
    from . import services as svc
    if reservation.status!="active":raise svc.Conflict("This reservation is already closed.")
    balance=m.StockBalance.objects.select_for_update().get(pk=reservation.balance_id)
    balance.reserved-=reservation.quantity-reservation.consumed
    reservation.status="released";svc.touch(balance);svc.touch(reservation);svc.record_event(reservation,actor,"stock.reservation_released")
    return reservation


def consume_order_reservations(tenant,order,item,warehouse,quantity):
    from . import services as svc
    if not order:return
    reservations=m.Reservation.objects.select_for_update().filter(tenant=tenant,order=order,balance__item=item,balance__warehouse=warehouse,status="active").order_by("created_at","id")
    remaining=quantity
    for reservation in reservations:
        take=min(remaining,reservation.quantity-reservation.consumed)
        balance=m.StockBalance.objects.select_for_update().get(pk=reservation.balance_id)
        balance.reserved-=take;reservation.consumed+=take
        if reservation.consumed==reservation.quantity:reservation.status="consumed"
        svc.touch(balance);svc.touch(reservation);remaining-=take
        if remaining==0:break


def reverse_movement(movement,actor,reason):
    from . import services as svc
    if movement.document_line_id or movement.transfer_id:raise ValidationError("Use the source document's return flow for a receipt, dispatch or transfer.")
    if m.StockMovement.objects.filter(reversal_of=movement).exists():raise svc.Conflict("This movement has already been reversed.")
    reverse=svc.move_stock(tenant=movement.tenant,actor=actor,item=movement.item,warehouse=movement.warehouse,quantity=-movement.quantity,
        unit_cost=movement.unit_cost,kind="reversal",reason=reason,bucket=movement.bucket,job=movement.job)
    reverse.reversal_of=movement;reverse.save(update_fields=["reversal_of"])
    for original in m.ManagementFact.objects.filter(tenant=movement.tenant,source_type="stockmovement",source_id=str(movement.id)):
        svc.fact(reverse,original.kind,-original.amount,f"Reverse {original.description}",original.category,job=original.job)
    return reverse


def open_count(count):
    for balance in m.StockBalance.objects.filter(tenant=count.tenant,warehouse=count.warehouse).order_by("id"):
        m.StockCountLine.objects.create(tenant=count.tenant,branch=count.branch,created_by=count.created_by,count=count,balance=balance,expected=balance.on_hand,balance_version=balance.version)


def post_count(count,actor):
    from . import services as svc
    if count.status not in ("draft","approved"):raise svc.Conflict("This count is no longer a draft or approved count.")
    if not count.lines.exists():raise ValidationError("There are no stock positions in this warehouse to count.")
    for line in count.lines.select_related("balance__item","balance__warehouse").order_by("balance_id"):
        balance=m.StockBalance.objects.select_for_update().get(pk=line.balance_id)
        if line.counted is None:raise ValidationError("Enter a counted quantity for every line.")
        if balance.version!=line.balance_version:raise svc.Conflict("Stock moved after this count started. Start a fresh count instead of overwriting current stock.")
        difference=line.counted-line.expected
        if difference:
            svc.move_stock(tenant=count.tenant,actor=actor,item=balance.item,warehouse=balance.warehouse,quantity=difference,
                unit_cost=balance.value/balance.on_hand if balance.on_hand else balance.item.purchase_rate,kind="adjustment",reason=count.reason,bucket=balance.bucket)
    count.status="posted";svc.touch(count);svc.record_event(count,actor,"stock.count_posted");return count
