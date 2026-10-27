# Bol Khata mobile app

An installable mobile web app (PWA) for Android and iPhone. This is not an APK or an App Store build. Each owner creates one store, signs in and uses a real server-side SQLite database. No accounts, customers, balances or transactions are seeded.

## Run locally

Requires Python 3.11+ with its standard library; no npm install. Ledger and chat work without external credentials; SMS/email delivery requires provider credentials.

```sh
cd bol-khata-github
python3 server.py
```

Open `http://127.0.0.1:8787`. Create an account, name your store and add customers. The database is created at `data/khata.sqlite3`, survives server restarts, and is not browser local storage. New installations start empty; the running installation retains its existing records.

Owners see only their store's data. Add optional customer phone numbers, emails and voice aliases, enter opening balances, record sales and payments with a method, choose credit due dates, draft bilingual reminders and download reports or a CSV ledger. The same sign-in accesses the same database from different devices once hosted.

Standalone payments allocate to the oldest active credit entry first. Payments entered with a sale apply to that sale. An opening balance is an owner-entered receivables adjustment, not a new sale. Amounts are integer paise in the database. Undo requires a reason, preserves the original entry and prevents a correction that would leave later payments without sufficient credit.

## Free local chat and voice

The app supports Qwen3 4B Instruct for conversation and multilingual Whisper Small for transcription. Models and runtimes are installed separately and are not included in this repository. Both can run locally without a paid AI API; they consume computer memory, electricity and processing time. Keep the app server and model service running while using them.

To enable local AI on a new computer:

1. Install Ollama from https://ollama.com/download and keep it running.
2. Download the model: `ollama pull qwen3:4b-instruct`.
3. Copy `.env.example` to `.env`. Its default AI endpoint is Ollama's local port 11434.
4. Start or restart the app with `python3 server.py` (macOS/Linux) or `py server.py` (Windows).

Without a running configured model, the app uses its basic command parser. Typed bookkeeping forms remain available. AI model installation requires an initial download; using the local model requires no paid AI API key.

Optional local speech needs whisper.cpp's `whisper-cli` and the multilingual `ggml-small.bin`. Set their absolute paths in `WHISPER_CLI` and `WHISPER_MODEL` in `.env`. On Windows use a Windows executable and paths, for example `C:/tools/whisper/whisper-cli.exe`. If these are absent, voice falls back to browser recognition when supported.

`start-local.py` and `start-local.sh` are retained as development launchers. Their `LLAMA_URL` branch expects the original developer's separately installed macOS runtime in `../../work/local-ai`; that runtime is not part of this repository. For a portable setup use Ollama and `server.py` as above. Do not copy another machine's private `.env`.

Chat understands conversational English, Hindi and Hinglish through the local model. Clear requests execute directly; replies describe actual server results. Missing or ambiguous information still requires a short question (for example a payment without its method). Chat history belongs to the signed-in session and survives reload. Stable request identifiers prevent duplicate execution when the same request is retried.

Examples:

- `Add customer Ravi phone 9876543210`
- `Ravi gave me two hundred rupees through UPI`
- `रवि ने दो सौ रुपये नकद दिए`
- `Change Ravi outstanding to 500 because the old opening entry was wrong`
- `Send a follow-up to Ravi` / `Send follow-ups to all`
- `Turn off automatic follow-ups`
- `Set automatic follow-ups to email, after 11, repeat every 4 days`
- `How much does Ravi owe?` / `Daily collection report`

The full-screen chat fixes its header and composer while the conversation scrolls. Replies are spoken by the device's speech synthesis; the speaker button mutes them. With local speech configured, microphone recording stops after approximately 1.5 seconds of silence, transcribes on the server, and submits automatically. Max recording duration is 30 seconds. Customer names supply hints to Whisper. Recognition quality depends on voice, background noise and hardware; this is not a guarantee of ChatGPT-level accuracy. Review the transcript and saved response/history. If local speech is unavailable the browser speech service is the fallback and may send audio to its own provider. Typing always remains available.

Actual phone microphone capture, spoken playback and accent performance require a physical-device check. The in-app preview returned a speech-playback error, so spoken playback was not verified there. Local Whisper was verified with a sample WAV; speech-end/cancellation and WAV conversion were tested separately. HTTPS is required on phones. Financial writes need an online server connection; no offline ledger queue or private-data cache is used.

Customer detail offers **Edit outstanding** with a mandatory reason and optional due date. This records before/after values and the difference in history. Corrections affect receivables but do not count as sales or received money. The dashboard shows seven-day sales/payment bars and an outstanding-age breakdown.

## Host for phone installation

A `localhost` URL on your computer cannot be used by other owners or phones. Deploy this directory to a Python host with a **persistent disk**, and serve it behind a reverse proxy that terminates HTTPS. A single app instance with SQLite is the intended deployment. Do not put SQLite on an ephemeral filesystem or run replicas with separate database files.

Set these environment variables using the actual deployment values:

```text
HOST=0.0.0.0
PORT=8787
PUBLIC_ORIGIN=https://your-domain.example
KHATA_DATABASE=/persistent-disk/khata.sqlite3
```

The proxy must preserve requests, cookies, and the browser's Origin header, and expose only the HTTPS app. Set `PUBLIC_ORIGIN` to the exact origin with no path; it controls CSRF origin checks and secure cookies. Direct non-loopback HTTP access is intentionally unsupported. Add proxy request limits and rate limits before public rollout. No hosting account, domain, deployment or store publication is included in this build.

Install from Chrome on Android using **Install app**, or from Safari on iPhone using **Share → Add to Home Screen**. Voice availability differs by browser. Installation and microphone capture must be checked on the target phones.

## Security and data operations

Passwords are scrypt-hashed with per-account salts. Session tokens are random, stored hashed, expire after seven days, and travel in HttpOnly SameSite=Strict cookies (Secure when HTTPS is configured). Authenticated writes require CSRF tokens. Store ownership is derived on the server from the session, never from a client-provided store ID. SQL is parameterized, ledger mutations are atomic and duplicate transaction requests are idempotent. Account-attempt limits are stored in SQLite. CSV string fields are escaped against spreadsheet formula injection.

Keep the database directory on restricted storage. SQLite is not encrypted by this app; use encrypted disks and secured backups for real customer data. Database access is restricted by owner in the app, but the server administrator can access database files. This build has no password reset or email verification, staff accounts, automated WhatsApp sending, inventory, GST filing, refunds, outgoing expenses or a complete cash-closing report. Account recovery and operational review are required before a broad public launch.

To back up safely while the app runs, use SQLite's backup API rather than copying only the `.sqlite3` file (WAL may contain recent writes):

```python
import sqlite3
with sqlite3.connect('data/khata.sqlite3') as source:
    with sqlite3.connect('/secure-backup/khata.sqlite3') as destination:
        source.backup(destination)
```

Store backups privately and test restoration. Never commit database files or credentials. CSV exports include ledger history but are not a full account/database backup.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Tests create disposable databases outside the live database and cover empty accounts, owner isolation, cross-store writes, payments, concurrent overpayment prevention, duplicate requests, corrections, dates, persistence and authentication.

## Manual and automatic reminders

The Follow-up screen offers manual reminders and a per-store automatic schedule
(hour in India time, 08–20, and repeat interval 1–30 days). Add a phone/email and
record the customer's permission for the selected channel. Optional email is
available when creating and editing customers. Customers created through chat do
not automatically grant messaging permission; edit their details to record it.

Delivery uses server-side providers, configured through environment variables:

- SMS: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`.
- Email: `RESEND_API_KEY`, `EMAIL_FROM` (a sender on a verified domain).

Never put API credentials in the page or commit them. Configure credentials in
your host's secret settings, then restart the Python server. Provider availability
means credentials are present, not that their validity or carrier delivery was
verified. Follow your provider's sender and destination onboarding requirements,
including support for sending to Indian mobile numbers. Charges, Unicode SMS
segments and country restrictions are controlled by the provider.

[Twilio message API](https://www.twilio.com/docs/messaging/api/message-resource)
and [Resend email API](https://resend.com/docs/api-reference/emails/send-email).

Requests persist in SQLite. The scheduler runs every 30 seconds while the server
runs; it checks current balances, contacts and consent before submitting. Auto sends happen between the configured hour and 20:59 IST. Changed
balances cancel stale queued messages. Requests are deduplicated, reminders have
a 24-hour minimum gap per customer/channel, and auto mode also uses the chosen
repeat interval. Clearing a balance or disabling auto mode prevents future auto
sends. Auto reminders target due/overdue balances only. Requests already submitted
to a provider cannot be recalled by recording a later payment.

History shows **queued**, **sending**, **accepted**, **failed**, **uncertain** or
**cancelled**. Accepted means the provider accepted a submission, not delivered.
This version does not ingest delivery webhooks. Check provider records for final
delivery. An uncertain send or a server restart during sending is never retried
automatically; review provider records before retrying. No real messages were sent
during development checks; sending was tested with an injected fake provider.

## Language and verification

The language setting translates controls and generated reports between Hindi and English. Names, entered content and historical messages remain as recorded.

```sh
python3 -m unittest discover -s tests -v
node tests/test_chat.js
```

Tests use separate databases and fake messaging providers. They verify store isolation, payment rules, direct actions, method clarification, replay protection, balance correction history, follow-up eligibility/settings and browser speech cancellation/WAV encoding.

## Windows quick start

Install Python 3.11 or newer, extract or clone this folder, and open a terminal inside the folder containing `server.py`:

```bat
py server.py
```

If `py` is unavailable, use `python server.py`. Keep the terminal open and visit http://127.0.0.1:8787. Create a new store account. No npm install, pip dependencies or separate database installation is required for the main app. Optional AI, speech and messaging require the separate setup described above.

## Repository contents

- `server.py`: HTTP API, authentication, SQLite ledger, reports and migrations.
- `assistant.py`: local model integration, command parsing and validated agent actions.
- `messaging.py`: SMS/email adapters, persistent queue and automatic follow-ups.
- `static/`: complete frontend, translations, icons, PWA manifest and service worker.
- `tests/`: Python backend tests and JavaScript audio/voice tests (Node.js needed only for the JS tests).
- `.env.example`: configuration template with no real credentials.
- `.gitignore`: excludes private configuration, databases, model weights and generated files.

No customer records, accounts, secrets, downloaded model weights or machine-specific private configuration are included. The app creates a fresh database on first run. Do not upload your live app folder's `data/` or `.env` to GitHub.
