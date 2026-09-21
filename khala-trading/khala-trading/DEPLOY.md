# Deploying KHALA TRADING to Google Cloud Run

Everything below assumes you're on your Kali Linux machine. Follow it in order.

## 1. One-time setup (skip if already done)

Install the Google Cloud CLI:
```bash
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
gcloud init
```
This opens a browser login -- sign in with the Google account tied to your GCP project.

Set your project (replace with your actual project ID -- find it at https://console.cloud.google.com):
```bash
gcloud config set project YOUR_PROJECT_ID
```

Enable the required services (only needs to be done once per project):
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com
```

## 2. Get your project files onto your machine

If I've given you this project as a downloadable folder, unzip it somewhere, e.g.:
```bash
cd ~
mkdir -p projects
cd projects
# unzip/copy the khala-trading folder here
cd khala-trading
```

## 3. Add your API keys

Copy the example env file and fill in your real keys:
```bash
cp .env.example .env
nano .env
```
Fill in:
- `GEMINI_API_KEY` -- from https://aistudio.google.com/apikey
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` -- from @BotFather (optional, skip if you don't want alerts yet)
- `ADMIN_PASSWORD` -- pick a real password for the admin portal
- `FLASK_SECRET_KEY` -- any long random string, e.g. run `openssl rand -hex 32` and paste the output

Save and exit (Ctrl+O, Enter, Ctrl+X in nano).

**Important:** `.env` is never read automatically by Cloud Run -- you pass these values in during deploy (next step), not by uploading the file itself. The `.env` file is just for your own local reference and local testing.

## 4. (Optional) Test it locally first

```bash
pip install -r requirements.txt --break-system-packages
export $(cat .env | xargs)
python3 app.py
```
Visit `http://localhost:8080` in your browser. Ctrl+C to stop when done checking.

## 5. Deploy to Cloud Run

From inside the `khala-trading` folder, run:

```bash
gcloud run deploy khala-trading \
  --source . \
  --region europe-west2 \
  --allow-unauthenticated \
  --set-env-vars="GEMINI_API_KEY=YOUR_GEMINI_KEY,TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN,TELEGRAM_CHAT_ID=YOUR_CHAT_ID,ADMIN_PASSWORD=YOUR_ADMIN_PASSWORD,FLASK_SECRET_KEY=YOUR_SECRET_KEY"
```

Replace each `YOUR_...` with the real values from your `.env` file. Region can be whatever's closest to you -- `europe-west2` (London) was used in the example.

This single command will:
1. Build a container image from your Dockerfile (via Cloud Build)
2. Push it to Google's container registry
3. Deploy it as a Cloud Run service
4. Give you a live URL when it finishes (looks like `https://khala-trading-xxxxx.europe-west2.run.app`)

First deploy takes 2-4 minutes. You'll see build logs streaming in your terminal.

## 6. Verify it's live

Once it finishes, it prints a **Service URL**. Open that in your browser -- you should see the dashboard load, with live TradingView charts and your signal engine running.

## 7. Redeploying after changes

Any time you edit the code, redeploy with the exact same command from step 5 (Cloud Run will build a new revision and switch traffic to it automatically, with zero downtime).

## Troubleshooting

- **"Permission denied" errors**: run `gcloud auth login` again.
- **Build fails on requirements**: double check `requirements.txt` has no typos.
- **App deploys but shows errors in browser**: check logs with:
  ```bash
  gcloud run services logs read khala-trading --region europe-west2 --limit 50
  ```
- **Price data isn't loading**: Yahoo Finance occasionally rate-limits server IPs; this is expected occasionally and the app is built to degrade gracefully (cached data, error messages instead of a crash).
