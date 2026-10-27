# Incrementum

**An Intelligent Big Data Platform for Cause-Aware Offer Generation, Smart Notification Optimization, and Customer Segmentation in Offline Retail** (academic project)

Incrementum studies what each customer buys, matches it to the offers that are running, sends a personal message (WhatsApp), and then checks who actually
responded. Python does all cleaning, analysis, segmentation, offer logic and notifications; Hadoop HDFS and Apache Spark demonstrate the Big Data layer;
SQLite stores the application data; **Power BI is the only analytics dashboard**; a small web site operates the campaigns.

## What is real, synthetic, simulated, local, or Hadoop/Spark
| Part | Status |
|---|---|
| Kaggle *Online Retail II* (1,067,371 rows, UK online gift-ware retailer, 2009-12 to 2011-12) | **Real** |
| Cleaning, features, RFM, k-means, product-preference segments, pair rules | Real data, **local Python** (Pandas, scikit-learn) |
| Raw and cleaned files in HDFS (`/incrementum/raw`, `/cleaned`, `/processed`) | **Real HDFS** (single node) |
| `process --mode spark`: PySpark DataFrames + Spark SQL reading from HDFS | **Real Spark, local[*] mode on one machine - not a cluster** |
| `process --mode local` | Local Pandas (Mode B); never labelled Hadoop/Spark |
| Five sample customers (Merin, Delna, Hanna, Angel, Adwoith), their purchases, a supermarket product list, offers and discount rules | **Synthetic / sample** (`is_synthetic=1`) |
| Product categories for the Kaggle products | **Derived** from product names by keyword rules (the dataset has no category column) |
| Treatment/control experiment and the Persuadable / Sure Thing / Lost Cause / Do-not-disturb groups | **Simulated** (control = real purchases; the notified group's response and opt-outs are simulated). Labels are marked *insufficient* when the validation check fails |
| WhatsApp messages | **Test mode by default** (nothing leaves the computer); real sending through the Twilio WhatsApp sandbox only when switched on |
| Power BI report (`.pbix`) | **Not included** - built from `powerbi/dashboard_guide.md` |

The dataset has no phone numbers, names, consent records, campaign history, stock levels or supermarket products.

## Project structure
```
src/
  config.py  database.py                     settings, SQLite access (schema: sql/schema.sql)
  data_cleaning.py  loader.py                Kaggle file -> cleaned files -> database
  hdfs_manager.py  spark_processing.py  local_processing.py     HDFS commands, Spark (Mode A), Pandas (Mode B)
  feature_engineering.py  product_segmentation.py  categorization.py   customer features, segments, categories
  cause_based_analysis.py                    simulated experiment + response groups
  offers.py  preferences.py  offer_generation.py  campaign_engine.py   offers, matching, proposals, one-click campaigns
  notification_service.py  whatsapp_tools.py  providers (test mode, Twilio WhatsApp), setup checks
  powerbi_export.py                          tables for Power BI
  demo_data.py                               the synthetic sample customers
  auth.py                                    accounts, password hashing, sessions
  webapp.py  main.py                         the website and the command line
config/      offers, recipients, discount rules (JSON)
tests/       automated tests
sql/         database schema
powerbi/     dashboard build guide        notebooks/   data exploration notebook        VIVA.md   viva questions and answers
.vscode/     VS Code settings and tasks (run the website, run the tests)
```

## Quick start (Ubuntu, Python 3.11 virtual environment)
```bash
python -m venv venv && source venv/bin/activate          # or: uv venv --python 3.11 venv
pip install -r requirements.txt
cp .env.example .env                                      # private settings; never committed
python -m pytest -q                                       # run the tests
python -m src.main setup-demo                             # create the sample customers and load offers
python -m src.main create-user --username YOURNAME --role admin     # first account (password asked, hidden)
python -m src.main web                                    # website at http://127.0.0.1:8000  (sign in with that account)
```
In VS Code (Remote-SSH to the machine that holds the project): open the folder, then *Terminal > Run Task > Run website*, or run `python -m src.main web` in the
terminal. VS Code forwards port 8000 (see the *Ports* tab); open `http://localhost:8000`. Without VS Code:
`ssh -L 8000:127.0.0.1:8000 <user>@<host>` and browse to `http://localhost:8000`.

## Run order for the full pipeline
```bash
# data/raw/online_retail_II.csv  <- Kaggle "mashlyn/online-retail-ii-uci" (not stored in this repository; keep it unchanged)
python -m src.main clean                  # cleaning + reports + log   (outputs/reports, outputs/logs)
python -m src.main hdfs-upload            # real `hdfs dfs` commands (needs HDFS running: start-dfs.sh)
python -m src.main load-db                # chunked load into SQLite
python -m src.main process --mode local   # Mode B: Pandas
python -m src.main process --mode spark   # Mode A: PySpark + Spark SQL reading from HDFS (log: outputs/logs/spark_run.log)
python -m src.main segments               # features, RFM, k-means, product-preference segments, pair rules
python -m src.main experiment             # SIMULATED treatment/control + response groups
python -m src.main export-powerbi         # tables for Power BI -> data/powerbi_exports
python -m src.main inspect-db             # tables and row counts
```
More: `customer --id C-MERIN`, `features --explain`, `categories`, `log`, `reset-campaigns`, `send-offers --category "All categories"`, `menu` (text menu), `whatsapp-check`, `whatsapp-test --name Merin`.

## Accounts and sign-in
Every page needs a signed-in account; there is no sign-up page and no default password. Create the first admin in the terminal, then admins add more accounts on the **Users** page (or with `create-user`):
```bash
python -m src.main create-user --username merin --role admin --name "Merin"    # password is asked for twice, hidden
python -m src.main list-users
python -m src.main set-password --username merin                               # also unlocks a locked account
```
| | admin | staff |
|---|---|---|
| Home, Send Offers (test mode / practice), Generate Offers, Customer Analysis, Segments, Campaign Results, Notifications | yes | yes |
| Send to phones via WhatsApp | yes | no |
| Offers, Recipients (consent), Setup (WhatsApp), Users | yes | no |

Security: passwords are stored only as salted scrypt hashes; five wrong passwords lock an account for five minutes; error messages never reveal whether a username exists; sessions are random tokens in an
HttpOnly cookie (only their SHA-256 hash is stored) and expire after 12 hours; a password change signs that person out everywhere; the last active admin cannot be disabled or demoted; sign-in activity is logged
on the Users page; account data is never exported to Power BI. The site listens on 127.0.0.1 only; it uses plain HTTP, so put it behind HTTPS if it is ever exposed beyond this machine.

## The website (operations site, not an analytics dashboard)
**Sign-in page** - **Home** (charts and overview) - **Send Offers** (All categories or one category; preview of who gets what; one click) - **Generate Offers** (analyse, propose, approve/reject, send approved) -
**Customer Analysis** - **Segments** (with the reason for each membership) - **Campaign Results** (who bought after a notification; the simulated experiment and response groups, each clickable) -
**Notifications** (full log, delivery check) - **Offers** (add / deactivate) - **Recipients** (consent) - **Setup** (WhatsApp checklist, login check, test message) - **Users** (accounts, admin only).

Honesty built in: a *Test mode* badge whenever messages are not really sent; a *Simulated* tag on experiment results; "accepted by provider" is never shown as "delivered".
Safety: test mode by default; sending to phones needs `WHATSAPP_ENABLED=true`, a confirmation tick and consenting recipients; duplicates are blocked per campaign and customer; every form has a
per-run token; the Host header is checked; text is HTML-escaped; phone numbers are never stored in the database or exports.

## Showing it more than once (reset between presentations)
Sending is protected against duplicates: clicking **Send Offers** a second time never messages the same people again, and this survives closing and restarting the website (everything is stored in the database).
To show the same send again to the next judge, run this **in the terminal** (it is deliberately not a button on the site):
```bash
python -m src.main reset-campaigns          # type RESET when asked (or add --yes)
```
It clears the message log, campaigns, offer proposals and purchases recorded at the counter, so **Send Offers** works again from scratch. It **keeps** consent, offers (including ones you switched off), customers,
segments, experiment results and the sample purchase history, so nothing needs to be set up again, and no website restart is needed - just reload the page. The old log is archived first to
`outputs/logs/archive/reset_<time>/` as CSV files (private; not committed to Git). A new calendar day also allows sending again without a reset. **Practice run** on the Send Offers page is test mode and can be repeated any time without a reset.
Real WhatsApp messages already sent stay in Twilio; each live send uses trial credit.

## Sending real WhatsApp messages (Twilio sandbox) - needs your own free Twilio account
1. `pip install twilio`.
2. In the Twilio console open the WhatsApp **sandbox** page. Note the sandbox number and your join keyword.
3. **Every recipient** sends `join <keyword>` to the sandbox number from their own WhatsApp. The join lasts 3 days.
4. **Within 24 hours before sending**, every recipient must also have sent any message (for example `hi`) to the sandbox number. WhatsApp only allows free-form messages (like offers)
   inside that 24-hour window; outside it only approved templates work, and the sandbox cannot use custom templates.
5. Fill `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` (the sandbox number), the five `DEMO_PHONE_*` numbers (`+91XXXXXXXXXX`), and `WHATSAPP_ENABLED=true`. Restart the website.
6. Check: `python -m src.main whatsapp-check` (or the **Setup** page): every row should be ready and the Twilio login should read OK. Send yourself a test message
   (`whatsapp-test --name <you>` or the Setup page), then use **Send Offers > Send to phones via WhatsApp**. Use **Notifications > Check delivery status** afterwards.
The sandbox sends one message every 3 seconds, so a live send to five people takes about 15 seconds. This path has been tested here only against a stand-in for the Twilio library, not the real service.

## Tests
`python -m pytest -q` (174 tests in the build environment: cleaning, features, segments, experiment, campaign rules, consent, duplicate protection, the website, the WhatsApp provider code against a stand-in
library, accounts and sign-in, Power BI export). Record your own result: ____ passed on ____.

## Troubleshooting
- Cannot sign in: after five wrong passwords an account is locked for five minutes; an admin can set a new password on the Users page, or run `set-password --username <name>` in the terminal. "No accounts exist yet" on the login page means you still need `create-user`.
- Website not reachable from Windows: use the VS Code Ports tab or the SSH tunnel above; port busy: `--port 8001`.
- No message on the phone: open Setup (or `whatsapp-check`). Typical causes: `WHATSAPP_ENABLED` still false (restart after editing `.env`), `twilio` not installed, the phone has not joined the sandbox, or it has not
  messaged the sandbox in the last 24 hours (error 63016).
- Stalled downloads: turn IPv6 off, use `tmux`, set `UV_HTTP_TIMEOUT=300`. HDFS not running: `start-dfs.sh`; never run `hdfs namenode -format` on an existing NameNode.
- `Killed` during `clean` or Spark: out of memory; give the VM more RAM or use `clean --sample 100000`.
- Sample offers not matching: the sample purchase history ages (90-day window). Run `python -m src.main setup-demo --reset` (removes only sample data) then `python -m src.main segments`,
  and record consent again on the Recipients page. `--wipe-all` deletes the whole database and asks first.

## Limitations and future work
Spark runs on one machine; gift-ware categories are keyword rules (about a third of sales stay "Other / Uncertain"); the experiment is simulated and its uplift model failed validation at about 400
customers per arm (a real campaign needs thousands per arm); no stock data, so offers ignore availability; no frequency-cap engine beyond duplicate protection; the WhatsApp sandbox is for testing
(a real shop would need an approved WhatsApp Business sender); basket-based personalization and multi-node Spark/YARN are future work; the Power BI report must be built by hand.

## Dataset
Online Retail II (UCI / Kaggle, CC BY 4.0): https://archive.ics.uci.edu/dataset/502/online+retail+ii - not redistributed here because of its size.
