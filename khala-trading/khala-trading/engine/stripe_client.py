"""
Stripe billing integration. Uses the official `stripe` Python package
(added to requirements.txt) -- installed automatically by Render, no manual
setup needed on your end beyond adding your API keys as environment vars.
"""

import os
import stripe
import users

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')

STRIPE_PRICE_ID = os.environ.get('STRIPE_PRICE_ID', '')  # the recurring monthly price you create in Stripe
WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')


def create_checkout_session(email, success_url, cancel_url):
    """
    Creates a Stripe Checkout session for a monthly subscription.
    Returns (checkout_url, error).
    """
    if not stripe.api_key or not STRIPE_PRICE_ID:
        return None, 'Stripe is not configured (missing STRIPE_SECRET_KEY or STRIPE_PRICE_ID)'

    try:
        session = stripe.checkout.Session.create(
            mode='subscription',
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            customer_email=email,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return session.url, None
    except stripe.error.StripeError as e:
        return None, str(e)


def construct_webhook_event(payload, sig_header):
    """
    Verifies and parses an incoming Stripe webhook. Raises ValueError or
    stripe.error.SignatureVerificationError on invalid payloads -- callers
    should catch and return 400 to Stripe.
    """
    return stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)


def handle_webhook_event(event):
    """
    Given a verified Stripe event, update our local user records accordingly.
    Returns a short string describing what happened, for logging.

    Handles the events that matter for a simple subscription flow:
      - checkout.session.completed: link the Stripe customer to our user
        and mark them active
      - customer.subscription.updated: sync status (active, past_due, etc.)
      - customer.subscription.deleted: mark canceled
      - invoice.payment_failed: mark past_due
    """
    event_type = event['type']
    obj = event['data']['object']

    if event_type == 'checkout.session.completed':
        email = obj.get('customer_details', {}).get('email') or obj.get('customer_email')
        customer_id = obj.get('customer')
        subscription_id = obj.get('subscription')
        if email and customer_id:
            users.set_stripe_customer(email, customer_id)
            users.update_subscription_status(
                email=email, customer_id=customer_id,
                subscription_id=subscription_id, status='active',
            )
            return f'Activated subscription for {email}'
        return 'checkout.session.completed missing email or customer_id'

    if event_type == 'customer.subscription.updated':
        customer_id = obj.get('customer')
        status = obj.get('status')  # active, past_due, canceled, unpaid, etc.
        users.update_subscription_status(customer_id=customer_id, status=status)
        return f'Updated subscription status to {status} for customer {customer_id}'

    if event_type == 'customer.subscription.deleted':
        customer_id = obj.get('customer')
        users.update_subscription_status(customer_id=customer_id, status='canceled')
        return f'Marked subscription canceled for customer {customer_id}'

    if event_type == 'invoice.payment_failed':
        customer_id = obj.get('customer')
        users.update_subscription_status(customer_id=customer_id, status='past_due')
        return f'Marked subscription past_due for customer {customer_id}'

    return f'Ignored event type {event_type}'
