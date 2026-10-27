"""Incrementum operational interface (command line).

  python -m src.main menu            interactive menu (Section A and Section B)
  python -m src.main setup-demo      create DB, seed synthetic demo data, load configured offers
  python -m src.main send-offers --category "Non-Food & Household" [--live] [--rehearsal]
  ... see `python -m src.main --help`
"""
import argparse
import sys
from datetime import date

from . import campaign_engine as ce, categorization, config, database, demo_data, offer_generation as og
from . import notification_service as ns, offers as offers_mod


# ------------------------------------------------------------------ printing
def table(rows, cols):
    rows = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def print_summary(s):
    label = "REHEARSAL (simulated)" if s["rehearsal"] else ("LIVE (WhatsApp)" if s["mode"] == "whatsapp" else "SIMULATION")
    print(f"\n=== Campaign summary - {label} - id {s['campaign_id']} ===")
    for k, name in [("eligible_customers", "Eligible (consented) customers"), ("processed", "Customers processed"),
                    ("simulated", "Notifications SIMULATED (not sent)"),
                    ("accepted_by_provider", "Messages ACCEPTED by provider (not proof of delivery)"),
                    ("delivered", "Confirmed deliveries"),
                    ("skipped_no_offer", "Customers skipped (no suitable active offer)"),
                    ("suppressed", "Suppressed (consent / duplicate / phone)"), ("failed", "Failed attempts")]:
        print(f"  {name:<58}{s[k]}")
    for reason, n in s["suppressed_reasons"].items():
        print(f"      - {n} x {reason}")
    if "active_offers_available" in s:
        print(f"  Active offers available for matching: {s['active_offers_available']}; "
              f"customers matched: {s['customers_matched']}")
    for m in s["messages"]:
        print(f"\n  -> {m['to']} [{m['status']}]\n     " + m["text"].replace("\n", "\n     "))
    print("\n  (Delivery is only 'confirmed' after the provider reports it: run `refresh-status`.)")


def ctx():
    conn = database.connect()
    return conn


# ------------------------------------------------------------------ actions
def cmd_setup_demo(a):
    if a.wipe_all and config.DB_PATH.exists():
        if input(f"This DELETES THE WHOLE DATABASE ({config.DB_PATH}) incl. Kaggle data. Type DELETE to continue: ") != "DELETE":
            print("Cancelled."); return
        config.DB_PATH.unlink()
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = database.connect()
    database.init_db(conn)
    if a.reset:
        print("Removed old demo data (Kaggle data kept):", demo_data.reset_demo(conn))
    print("Seeded SYNTHETIC demo data:", demo_data.seed(conn))
    print("Loaded configured offers:", offers_mod.load_from_config(conn))
    print("Product departments:", categorization.apply_to_products(conn))


def confirm_live():
    if not config.WHATSAPP_ENABLED:
        print("Real sending is DISABLED (WHATSAPP_ENABLED is not true in .env). Nothing was sent.")
        return False
    print("\n*** LIVE MODE: real WhatsApp messages will be sent to consenting demo recipients. ***")
    return input("Type SEND to continue: ").strip() == "SEND"


def cmd_send_offers(a, conn=None):
    conn = conn or ctx()
    mode = "whatsapp" if a.live and not a.rehearsal else "simulated"
    if mode == "whatsapp" and not confirm_live():
        return
    try:
        print_summary(ce.run_active_offer_campaign(conn, a.category, mode=mode, rehearsal=a.rehearsal))
    except (ce.LiveSendingDisabled, ns.ProviderConfigError) as e:
        print("Not sent:", e)


def show_offers(conn):
    rows = offers_mod.active_offers(conn, date.today())
    print(f"\nCurrently ACTIVE configured offers (today {date.today()}):")
    table([(o["offer_code"], o["department"], o["product_name"], offers_mod.offer_text(o), o["valid_to"]) for o in rows],
          ["code", "department", "product", "offer", "valid_to"])


def show_recipients(conn):
    print("\nDemo recipients -> synthetic customer profiles (phone numbers are in .env, not shown):")
    table([(r["name"], r["customer_id"], r["consent_status"], "yes" if ns.get_phone(r["phone_env_key"]) else "no")
           for r in ce.get_recipients(conn)], ["name", "profile", "consent", "phone set in .env"])


def cmd_consent(a, conn=None):
    conn = conn or ctx()
    if a.grant_all:
        if input("Have ALL listed team members agreed to receive demo messages? (yes/no): ").strip().lower() != "yes":
            print("No changes made.")
            return
        conn.execute("UPDATE demo_recipients SET consent_status='opted_in', consent_recorded_at=?", (database.now_iso(),))
    elif a.grant or a.revoke:
        name = a.grant or a.revoke
        status = "opted_in" if a.grant else "opted_out"
        if conn.execute("UPDATE demo_recipients SET consent_status=?, consent_recorded_at=? WHERE lower(name)=lower(?)",
                        (status, database.now_iso(), name)).rowcount == 0:
            print("Unknown recipient:", name)
            return
    conn.commit()
    show_recipients(conn)


def cmd_analyze(a, conn=None):
    conn = conn or ctx()
    analysis, groups = og.analyze_category(conn, a.category)
    print(f"\nPurchase-behaviour analysis for: {a.category}  (SYNTHETIC demo transactions)")
    rows = []
    for x in analysis:
        top = x["top"]
        rows.append((x["name"], top["description"] if top else "-", top["purchases"] if top else "-",
                     ", ".join(f"{p['description']} x{p['purchases']}" for p in x["preferences"]) or "none"))
    table(rows, ["customer", "top frequent product", "purchases", "all products in this department"])
    print("\nCustomer groups (by top product):")
    for prod, names in groups.items():
        print(f"  {prod}: {', '.join(names)}")


def show_proposals(conn, department=None):
    rows = og.list_proposals(conn, department)
    print("\nOffer PROPOSALS (not active until approved):")
    table([(r["proposal_id"], r["name"], r["product"], f"{r['discount_percent']:g}%", r["status"],
            "sent" if r["notified_at"] else "") for r in rows],
          ["id", "customer", "product", "discount", "status", "notified"])
    for r in rows:
        print(f"  #{r['proposal_id']}: {r['reason']}")


def cmd_propose(a, conn=None):
    conn = conn or ctx()
    created = og.propose_offers(conn, a.category)
    print(f"Created {len(created)} new proposal(s). They are NOT active and will NOT be sent until approved.")
    show_proposals(conn, a.category)


def parse_ids(text):
    return None if text in (None, "all") else [int(x) for x in text.split(",") if x.strip()]


def cmd_send_approved(a, conn=None):
    conn = conn or ctx()
    mode = "whatsapp" if a.live and not a.rehearsal else "simulated"
    if mode == "whatsapp" and not confirm_live():
        return
    try:
        print_summary(og.send_approved(conn, a.category, mode=mode, rehearsal=a.rehearsal))
    except (ce.LiveSendingDisabled, ns.ProviderConfigError) as e:
        print("Not sent:", e)


def cmd_log(a, conn=None):
    conn = conn or ctx()
    rows = conn.execute("SELECT log_id, attempted_at, recipient_name, department, status, delivery_mode, "
                        "COALESCE(skip_or_suppression_reason, failure_reason, product_or_category) AS detail "
                        "FROM notification_logs ORDER BY log_id DESC LIMIT ?", (a.last,)).fetchall()
    table([tuple(r) for r in rows], ["id", "time", "recipient", "department", "status", "mode", "detail"])


def cmd_refresh(a, conn=None):
    conn = conn or ctx()
    try:
        n = ce.refresh_delivery_status(conn, ns.get_provider("whatsapp"))
        print(f"{n} message(s) newly confirmed as delivered by the provider.")
    except ns.ProviderConfigError as e:
        print("Cannot check provider:", e)


def cmd_clean(a, conn=None):
    from . import data_cleaning
    conn = conn or ctx()
    if not config.RAW_FILE.exists():
        print("Raw file not found:", config.RAW_FILE); return
    print("Cleaning with Pandas on local files (Mode B - Hadoop/Spark are NOT used in this step)...")
    res = data_cleaning.run_cleaning(sample=a.sample, conn=conn)
    print("\nCleaning steps:")
    table([(s["step_order"], s["step_name"], s["rows_before"], s["rows_after"], s["rows_affected"]) for s in res.log],
          ["#", "step", "rows_before", "rows_after", "rows_affected"])
    print("\nBefore/after summary:"); table(res.reports["quality_summary"].values.tolist(), ["metric", "before", "after"])
    print("\nInvalid / removed rows by rule:"); table(res.reports["invalid_values"].values.tolist(), ["rule", "rows"])
    print(f"\nSaved: {config.CLEANED_DIR}  (cleaned_sales.csv, cleaned_guest_sales.csv, returns_cancellations.csv)")
    print(f"Reports: {config.REPORTS_DIR}   Log: {config.LOGS_DIR / 'cleaning_log.json'}")


def cmd_load_db(a, conn=None):
    from . import loader
    conn = conn or ctx()
    database.init_db(conn)
    print("Loading cleaned sales into the database (chunked)...")
    print(loader.load_cleaned_to_db(conn))
    cmd_inspect(a, conn)


def cmd_hdfs_upload(a):
    from . import hdfs_manager
    if not hdfs_manager.is_available():
        print("HDFS is NOT reachable (is start-dfs.sh running?). Nothing uploaded."); return
    for ok, cmd, out in hdfs_manager.upload_cleaned():
        print(("OK   " if ok else "FAIL ") + cmd + ("\n" + out if out else ""))


def cmd_process(a, conn=None):
    conn = conn or ctx()
    if a.mode == "spark":
        from . import spark_processing
        print("MODE A: PySpark (local[*]) reading from HDFS when available")
        try:
            spark_processing.run_spark(conn)
        except ImportError:
            print("pyspark is not installed in this environment. Use --mode local."); return
    else:
        from . import local_processing
        print("MODE B: LOCAL Pandas processing (Hadoop/Spark are NOT used)")
        local_processing.run_local(conn)
    print("Outputs in", config.PROCESSED_DIR)
    for n in ("sales_summary",):
        print(open(config.PROCESSED_DIR / f"{n}.csv").read())


def cmd_features(a, conn=None):
    from . import feature_engineering as fe
    print("Customer features - how each is calculated:\n")
    for k, v in fe.EXPLANATIONS.items():
        print(f"  {k:<26}{v}")
    print("\n" + fe.REFERENCE_NOTE)


def cmd_segments(a, conn=None):
    from . import product_segmentation as ps
    conn = conn or ctx()
    print("Computing customer features and segments (local Python: Pandas + scikit-learn)...")
    res = ps.run_segmentation(conn)
    ps.export_segmentation(conn, res["rules"])
    print(f"\nCustomers with features: {res['customers']:,}")
    if res["kmeans"]:
        print(f"k-means chose k={res['kmeans']['k']} (silhouette {res['kmeans']['silhouette']})")
        for name, d in sorted(res["kmeans"]["profiles"].items()):
            print(f"   {name}: {d}")
    print("\nSegments (one customer can be in several):")
    table([tuple(r) for r in conn.execute(
        "SELECT s.segment_type, s.segment_name, COUNT(m.customer_id) FROM product_preference_segments s "
        "LEFT JOIN customer_segment_memberships m USING(segment_id) GROUP BY s.segment_id "
        "ORDER BY s.segment_type, 3 DESC LIMIT 120")], ["type", "segment", "customers"])
    print(f"\nPair association rules found: {len(res['rules'])} (top 5 by lift)")
    print(res["rules"].head(5).to_string(index=False))
    print(f"\nExported CSVs to {config.PROCESSED_DIR}")


def cmd_customer(a, conn=None):
    from . import product_segmentation as ps
    conn = conn or ctx()
    ex = ps.explain_customer(conn, a.id)
    if not ex["features"]:
        print("No features for", a.id, "- run `segments` first, or check the id."); return
    print("Features:"); [print(f"  {k}: {v}") for k, v in ex["features"].items()]
    print("\nWhy this customer is in each segment:")
    for s in ex["segments"]:
        print(f"  [{s['segment_type']}] {s['segment_name']}\n      {s['reason']}")


def cmd_experiment(a, conn=None):
    from . import cause_based_analysis as cb
    conn = conn or ctx()
    print("Running the SIMULATED treatment/control experiment (control = real behaviour, treatment response simulated)...")
    res = cb.run_experiment(conn, category=a.category, window_days=a.window_days, seed=a.seed, max_lift=a.max_lift)
    eid = cb.save_experiment(conn, res)
    cb.export_experiment(conn, res, eid)
    st = res["stats"]
    print(f"\nCategory: {st['category']}   cut-off {st['cutoff']}   window ends {st['window_end']}")
    print(f"Eligible customers: {res['n_eligible']:,}   (simulated opt-outs excluded: {st['opted_out_excluded']})")
    print(f"Treatment n={st['n_treatment']:,}   Control n={st['n_control']:,}")
    print("\n1) OBSERVED purchases after the (simulated) notification:", st["observed_purchases_after_notification"],
          "- this alone does NOT show the message caused them")
    print(f"2) CONVERSION  treatment {st['conv_treatment']:.1%}  vs  control {st['conv_control']:.1%}   "
          f"difference {st['difference']:+.1%}  (95% CI {st['ci_low']:+.1%} to {st['ci_high']:+.1%}, p={st['p_value']:.4f})")
    print(f"3) ESTIMATED incremental purchases: {st['estimated_incremental_purchases']:.0f}  "
          "(randomised comparison, but the treatment response is SIMULATED, so this is a demonstration, not a finding)")
    if res["uplift_groups"] is not None:
        print("\nUplift model check (customers grouped by predicted uplift):")
        table([(int(r.predicted_uplift_quintile), r.n_treatment, r.n_control, f"{r.conv_treatment:.1%}", f"{r.conv_control:.1%}",
                f"{r.observed_uplift:+.1%}", f"{r.mean_predicted_uplift:+.1%}") for r in res["uplift_groups"].itertuples()],
              ["quintile", "n_treat", "n_ctrl", "conv_treat", "conv_ctrl", "observed_uplift", "predicted_uplift"])
    print("\nCause-based segments (evidence level shown):")
    table([tuple(r) for r in conn.execute("SELECT segment_label, evidence_level, COUNT(*) FROM cause_based_segments "
                                          "WHERE experiment_id=? GROUP BY 1,2 ORDER BY 1", (eid,))],
          ["segment", "evidence", "customers"])
    print()
    for n in res["notes"]:
        print("NOTE:", n)
    print(f"\nExported to {config.PROCESSED_DIR}")


def cmd_export_powerbi(a, conn=None):
    from . import powerbi_export
    conn = conn or ctx()
    m = powerbi_export.export_powerbi(conn)
    print(f"Power BI tables written to {config.POWERBI_DIR}\n")
    table(m[["table", "rows", "data_label"]].values.tolist(), ["table", "rows", "data_label"])
    print("\nCopy this folder to Windows and refresh Power BI (see powerbi/dashboard_guide.md).")


def cmd_whatsapp_check(a, conn=None):
    from . import whatsapp_tools as wt
    conn = conn or ctx()
    print("WhatsApp readiness:")
    for label, ok in wt.readiness(conn):
        print(f"  [{'OK' if ok else '--'}] {label}")
    ok, msg = wt.check_login()
    print(f"\nTwilio login: {'OK' if ok else 'NOT OK'} - {msg}")
    print("\nIMPORTANT:", wt.REMINDER)


def cmd_whatsapp_test(a, conn=None):
    from . import whatsapp_tools as wt
    conn = conn or ctx()
    confirmed = a.yes or input(f"Send a REAL WhatsApp test message to {a.name}? Type SEND to continue: ").strip() == "SEND"
    kind, text = wt.test_message(conn, a.name, confirmed)
    print({"ok": "OK: ", "warn": "NOT SENT: ", "err": "FAILED: "}[kind] + text)


def cmd_reset_campaigns(a, conn=None):
    conn = conn or ctx()
    print("This clears the message log, campaigns, offer proposals and purchases recorded at the counter, so Send Offers can be run again.\n"
          "KEPT: consent, offers, customers, segments, experiment results and the sample purchase history. The old log is archived first.")
    if not a.yes and input("Type RESET to continue: ").strip() != "RESET":
        print("Cancelled - nothing changed.")
        return
    r = demo_data.reset_campaigns(conn)
    print(f"Cleared: {r['messages']} message log rows, {r['campaigns']} campaigns, {r['proposals']} proposals, "
          f"{r['generated_offers']} generated offers, {r['counter_purchases']} counter purchases.")
    print("Archived to:", r["archive"] or "(nothing to archive)")
    print("Done. Reload the website page - no restart needed.")


def _ask_new_password():
    import getpass
    pw = getpass.getpass("Password (at least 8 characters, typed hidden): ")
    return pw if pw == getpass.getpass("Repeat password: ") else None


def cmd_create_user(a, conn=None):
    from . import auth
    conn = conn or ctx()
    database.init_db(conn)
    pw = _ask_new_password()
    if pw is None:
        print("The two passwords did not match. Nothing was created.")
        return
    try:
        auth.create_user(conn, a.username, pw, a.role, a.name)
        print(f"Account '{a.username}' created with role '{a.role}'. Sign in on the website's login page.")
    except ValueError as e:
        print("Not created:", e)


def cmd_list_users(a, conn=None):
    from . import auth
    conn = conn or ctx()
    database.init_db(conn)
    table([(u["username"], u["display_name"], u["role"], "active" if u["is_active"] else "disabled", u["last_login_at"] or "never")
           for u in auth.list_users(conn)], ["username", "name", "role", "status", "last sign-in"])


def cmd_set_password(a, conn=None):
    from . import auth
    conn = conn or ctx()
    database.init_db(conn)
    row = conn.execute("SELECT user_id FROM users WHERE username=?", (a.username,)).fetchone()
    if not row:
        print("No such account.")
        return
    pw = _ask_new_password()
    if pw is None:
        print("The two passwords did not match. Nothing was changed.")
        return
    try:
        auth.set_password(conn, row["user_id"], pw)
        print("Password changed. That account has been signed out everywhere.")
    except ValueError as e:
        print("Not changed:", e)


def cmd_inspect(a, conn=None):
    conn = conn or ctx()
    table([(t["table"], t["rows"], ", ".join(t["columns"][:6]) + ("..." if len(t["columns"]) > 6 else ""))
           for t in database.inspect_db(conn)], ["table", "rows", "columns"])


def cmd_categories(a, conn=None):
    conn = conn or ctx()
    if a.set:
        categorization.set_manual(conn, a.set[0], a.set[1])
        print("Updated", a.set[0])
    table([tuple(r) for r in conn.execute("SELECT stock_code, description, department, category_source, needs_review "
                                          "FROM products ORDER BY department, stock_code")],
          ["code", "description", "department", "source", "needs_review"])


# ------------------------------------------------------------------ menu
def pick_category():
    print("\nCategories:")
    for i, d in enumerate(config.DEPARTMENTS, 1):
        print(f"  {i}. {d}")
    try:
        return config.DEPARTMENTS[int(input("Choose category number: ")) - 1]
    except (ValueError, IndexError):
        print("Invalid choice.")
        return None


def menu():
    conn = ctx()
    category = config.DEPARTMENTS[0]
    ns_ = argparse.Namespace
    while True:
        mode_label = "LIVE ENABLED in .env" if config.WHATSAPP_ENABLED else "simulation only"
        print(f"\n================ INCREMENTUM  [{mode_label}] ================")
        print(f"SECTION A - Send Active Offers   (selected category: {category})")
        print("  1) View active offers      2) View recipients & consent    3) Choose category")
        print("  4) >>> SEND OFFERS <<<  (simulated)      5) SEND OFFERS (LIVE WhatsApp)")
        print("  6) Rehearsal run (simulated, repeatable) 7) View notification log")
        print("SECTION B - Analyze Customers and Generate Offers")
        print("  8) Analyze category   9) Generate proposals   10) Review / approve / reject")
        print(" 11) Send APPROVED offers (simulated)         12) Send APPROVED offers (LIVE)")
        print("  0) Quit")
        c = input("Choice: ").strip()
        if c == "0":
            return
        elif c == "1": show_offers(conn)
        elif c == "2": show_recipients(conn)
        elif c == "3": category = pick_category() or category
        elif c == "4": cmd_send_offers(ns_(category=category, live=False, rehearsal=False), conn)
        elif c == "5": cmd_send_offers(ns_(category=category, live=True, rehearsal=False), conn)
        elif c == "6": cmd_send_offers(ns_(category=category, live=False, rehearsal=True), conn)
        elif c == "7": cmd_log(ns_(last=20), conn)
        elif c == "8": cmd_analyze(ns_(category=category), conn)
        elif c == "9": cmd_propose(ns_(category=category), conn)
        elif c == "10":
            show_proposals(conn, category)
            act = input("approve / reject / back: ").strip().lower()
            if act in ("approve", "reject"):
                ids = parse_ids(input("Proposal ids (comma-separated) or 'all': ").strip())
                n = og.approve(conn, ids) if act == "approve" else og.reject(conn, ids)
                print(f"{n} proposal(s) {act}d.")
        elif c == "11": cmd_send_approved(ns_(category=category, live=False, rehearsal=False), conn)
        elif c == "12": cmd_send_approved(ns_(category=category, live=True, rehearsal=False), conn)
        else: print("Unknown choice.")


# ------------------------------------------------------------------ CLI wiring
def build_parser():
    p = argparse.ArgumentParser(prog="python -m src.main", description="Incrementum operational interface")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("setup-demo")
    s.add_argument("--reset", action="store_true", help="remove ONLY demo data/campaigns/logs and recreate the demo (keeps Kaggle data)")
    s.add_argument("--wipe-all", action="store_true", help="DANGEROUS: delete the whole database file (asks for confirmation)")
    for name in ("send-offers", "send-approved"):
        s = sub.add_parser(name)
        s.add_argument("--category", required=True,
                       choices=([config.ALL_CATEGORIES] if name == "send-offers" else []) + config.DEPARTMENTS)
        s.add_argument("--live", action="store_true", help="real WhatsApp sending (needs WHATSAPP_ENABLED=true)")
        s.add_argument("--rehearsal", action="store_true", help="simulated, repeatable, never live")
    for name in ("analyze", "propose"):
        s = sub.add_parser(name); s.add_argument("--category", required=True, choices=config.DEPARTMENTS)
    s = sub.add_parser("proposals"); s.add_argument("--category", choices=config.DEPARTMENTS)
    for name in ("approve", "reject"):
        s = sub.add_parser(name); s.add_argument("--ids", required=True, help="comma-separated ids or 'all'")
    s = sub.add_parser("consent")
    g = s.add_mutually_exclusive_group(); g.add_argument("--grant-all", action="store_true")
    g.add_argument("--grant"); g.add_argument("--revoke")
    sub.add_parser("offers"); sub.add_parser("recipients"); sub.add_parser("inspect-db")
    sub.add_parser("refresh-status"); sub.add_parser("menu")
    s = sub.add_parser("create-user", help="create a website account (password is asked for, hidden)")
    s.add_argument("--username", required=True); s.add_argument("--role", choices=["admin", "staff"], default="staff")
    s.add_argument("--name", help="display name")
    sub.add_parser("list-users", help="list website accounts")
    s = sub.add_parser("set-password", help="change an account's password (asked for, hidden)")
    s.add_argument("--username", required=True)
    s = sub.add_parser("reset-campaigns", help="clear messages/campaigns/proposals so Send Offers can run again (archives first)")
    s.add_argument("--yes", action="store_true", help="skip the typed RESET confirmation")
    sub.add_parser("whatsapp-check", help="check WhatsApp/Twilio setup (sends nothing)")
    s = sub.add_parser("whatsapp-test", help="send ONE real WhatsApp test message to a consenting recipient")
    s.add_argument("--name", required=True); s.add_argument("--yes", action="store_true", help="skip the typed SEND confirmation")
    sub.add_parser("load-db"); sub.add_parser("hdfs-upload"); sub.add_parser("segments"); sub.add_parser("export-powerbi")
    s = sub.add_parser("experiment")
    s.add_argument("--category", help="gift-ware category to test (default: the one with most eligible customers)")
    s.add_argument("--window-days", type=int, default=30); s.add_argument("--seed", type=int, default=42)
    s.add_argument("--max-lift", type=float, default=0.35, help="ASSUMED simulated response strength (not a finding)")
    s = sub.add_parser("features"); s.add_argument("--explain", action="store_true")
    s = sub.add_parser("customer"); s.add_argument("--id", required=True, help="e.g. DEMO-MERIN or a Kaggle id like 13085")
    s = sub.add_parser("process"); s.add_argument("--mode", choices=["spark", "local"], required=True)
    s = sub.add_parser("clean"); s.add_argument("--sample", type=int, help="clean only the first N rows (quick test)")
    s = sub.add_parser("web", help="open the operational web page (Send Offers button)")
    s.add_argument("--port", type=int, default=8000); s.add_argument("--open", action="store_true", help="open a browser on this machine")
    s = sub.add_parser("log"); s.add_argument("--last", type=int, default=20)
    s = sub.add_parser("categories"); s.add_argument("--set", nargs=2, metavar=("STOCK_CODE", "DEPARTMENT"))
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd is None:
        build_parser().print_help(); return
    conn = None if a.cmd in ("setup-demo", "menu", "web") else ctx()
    if a.cmd == "setup-demo": cmd_setup_demo(a)
    elif a.cmd == "menu": menu()
    elif a.cmd == "send-offers": cmd_send_offers(a, conn)
    elif a.cmd == "send-approved": cmd_send_approved(a, conn)
    elif a.cmd == "analyze": cmd_analyze(a, conn)
    elif a.cmd == "propose": cmd_propose(a, conn)
    elif a.cmd == "proposals": show_proposals(conn, a.category)
    elif a.cmd == "approve": print(og.approve(conn, parse_ids(a.ids)), "approved")
    elif a.cmd == "reject": print(og.reject(conn, parse_ids(a.ids)), "rejected")
    elif a.cmd == "consent": cmd_consent(a, conn)
    elif a.cmd == "offers": show_offers(conn)
    elif a.cmd == "recipients": show_recipients(conn)
    elif a.cmd == "log": cmd_log(a, conn)
    elif a.cmd == "refresh-status": cmd_refresh(a, conn)
    elif a.cmd == "inspect-db": cmd_inspect(a, conn)
    elif a.cmd == "clean": cmd_clean(a, conn)
    elif a.cmd == "create-user": cmd_create_user(a, conn)
    elif a.cmd == "list-users": cmd_list_users(a, conn)
    elif a.cmd == "set-password": cmd_set_password(a, conn)
    elif a.cmd == "reset-campaigns": cmd_reset_campaigns(a, conn)
    elif a.cmd == "whatsapp-check": cmd_whatsapp_check(a, conn)
    elif a.cmd == "whatsapp-test": cmd_whatsapp_test(a, conn)
    elif a.cmd == "load-db": cmd_load_db(a, conn)
    elif a.cmd == "features": cmd_features(a, conn)
    elif a.cmd == "segments": cmd_segments(a, conn)
    elif a.cmd == "experiment": cmd_experiment(a, conn)
    elif a.cmd == "export-powerbi": cmd_export_powerbi(a, conn)
    elif a.cmd == "web":
        from . import webapp
        webapp.serve(a.port, a.open)
    elif a.cmd == "customer": cmd_customer(a, conn)
    elif a.cmd == "hdfs-upload": cmd_hdfs_upload(a)
    elif a.cmd == "process": cmd_process(a, conn)
    elif a.cmd == "categories": cmd_categories(a, conn)


if __name__ == "__main__":
    main()
