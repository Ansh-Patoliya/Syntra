import razorpay
from django.conf import settings
from .models import Payment

def get_razorpay_client():
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise ValueError("Razorpay keys are not configured in settings.")
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

def create_razorpay_order(amount, team, user):
    """
    amount is in INR (rupees). Razorpay expects amount in paise, so we multiply by 100.
    """
    client = get_razorpay_client()
    
    order_amount = int(amount * 100)
    order_currency = 'INR'
    
    order_receipt = f"team_{team.id}_user_{user.id}"
    
    payment_data = {
        'amount': order_amount,
        'currency': order_currency,
        'receipt': order_receipt,
    }
    
    # Hit Razorpay API to create an order
    razorpay_order = client.order.create(data=payment_data)
    
    # Create the pending payment record
    payment = Payment.objects.create(
        team=team,
        user=user,
        amount=amount,
        razorpay_order_id=razorpay_order['id'],
        status='pending'
    )
    
    return razorpay_order, payment

def verify_razorpay_signature(order_id, payment_id, signature):
    """
    Verifies the signature using Razorpay SDK.
    Returns True if valid, raises error or returns False if invalid.
    """
    client = get_razorpay_client()
    params_dict = {
        'razorpay_order_id': order_id,
        'razorpay_payment_id': payment_id,
        'razorpay_signature': signature
    }
    try:
        client.utility.verify_payment_signature(params_dict)
        return True
    except razorpay.errors.SignatureVerificationError:
        return False
