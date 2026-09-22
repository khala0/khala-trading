# Setting Up Stripe Billing (Subscription Paywall)

This gates the AI Sniper Telemetry and Ledger tabs behind a paid monthly
subscription. The live chart stays free for anyone to view. Admin login
always bypasses the paywall entirely.

## 1. Create a Stripe account

Go to https://dashboard.stripe.com/register -- free to sign up, no cost
until you actually take payments (Stripe takes a small % + fee per
transaction, standard for the industry).

## 2. Create your subscription product

- In the Stripe Dashboard, go to **Product catalog** → **Add product**
- Name it something like "Khala Trading Pro"
- Under pricing, choose **Recurring**, set your price (e.g. $29/month),
  billing period **Monthly**
- Save it, then click into the product and copy the **Price ID**
  (looks like `price_1AbCdEfGhIjKlMnOp`)

## 3. Get your API key

- Go to **Developers** → **API keys**
- Copy the **Secret key** (starts with `sk_test_...` while in test mode,
  `sk_live_...` once you switch to live mode)
- **Use test mode first.** Stripe gives you fake card numbers (like
  `4242 4242 4242 4242`) to test the whole flow without real money.

## 4. Set up the webhook

Stripe needs a way to tell your app when someone actually pays (or their
payment fails, or they cancel). This happens via a webhook.

- Go to **Developers** → **Webhooks** → **Add endpoint**
- Endpoint URL: `https://YOUR-RENDER-URL.onrender.com/api/billing/webhook`
  (replace with your actual Render URL)
- Select these events to listen for:
  - `checkout.session.completed`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_failed`
- Save it, then click into the webhook and copy the **Signing secret**
  (starts with `whsec_...`)

## 5. Add these to Render's environment variables

In your Render dashboard, go to your service → **Environment**, and add:
- `STRIPE_SECRET_KEY` → your secret key from step 3
- `STRIPE_PRICE_ID` → your Price ID from step 2
- `STRIPE_WEBHOOK_SECRET` → your signing secret from step 4

Render will automatically redeploy with the new variables.

## 6. Test the whole flow

1. Visit your live site, click **Sign up**, create an account
2. Click into the **AI Sniper Telemetry** tab -- you should see the
   "Subscription Required" paywall
3. Click **Subscribe**, it redirects to Stripe Checkout
4. Use a Stripe test card: `4242 4242 4242 4242`, any future expiry date,
   any 3-digit CVC, any postal code
5. Complete checkout -- you'll be redirected back to your site
6. Refresh the Signal or Ledger tab -- it should now show real content
   instead of the paywall

If it doesn't unlock immediately, check **Developers** → **Webhooks** in
Stripe -- click your endpoint and look at recent deliveries for errors.

## 7. Go live

Once you've tested the full flow in test mode:
- Toggle Stripe's dashboard from **Test mode** to **Live mode** (top right)
- Repeat steps 2-4 in live mode (product, API key, webhook are separate
  between test and live)
- Update the three Render environment variables with your live values
- Real payments will now be processed

## How access control works

- Anyone can view the live chart, no login needed
- Signal and Ledger tabs require: logged in AND active subscription
- Your admin login (the existing Admin Portal password) bypasses the
  subscription check entirely -- you always have full access
