# Power BI dashboard guide (Incrementum)

**Status: no `.pbix` file is included.** Power BI Desktop (Windows only) was not available where this project was
built, so nothing here has been opened in Power BI. This guide gives the exact tables, relationships, measures and
pages to build it yourself. Python already did all cleaning, analysis, segmentation and offer logic; Power BI only displays.

## 1. Get the data into Power BI
1. In the VM: `python -m src.main export-powerbi` (writes `~/incrementum/data/powerbi_exports/`).
2. Copy the folder to Windows (PowerShell, not the SSH window):
   `scp -P 2222 -r merin@127.0.0.1:~/incrementum/data/powerbi_exports C:\incrementum_powerbi`
3. Power BI Desktop: **Home > Transform data > Manage parameters > New**: Name `ExportFolder`, Type Text,
   Current value `C:\incrementum_powerbi\`.
4. For each CSV: **Get data > Text/CSV** (or New Source > Blank Query, then in Advanced Editor use
   `Csv.Document(File.Contents(ExportFolder & "fact_sales.csv"), [Delimiter=",", Encoding=65001])`, promote headers).
   Load every CSV listed in `export_manifest.csv`. Power Query is used only for loading and setting data types.
5. Set types: dates (`invoice_date`, `date`, `purchase_date`, `valid_from`, `valid_to`) = Date; `attempted_at`,
   `created_at` = Date/Time; money and counts = Decimal/Whole number; `is_synthetic`, `is_simulated` = Whole number.
6. Mark `dim_date` as a date table (Table tools > Mark as date table > `date`).

**Refresh after re-running the pipeline:** run `export-powerbi` again, re-copy the folder to the same path, click
**Home > Refresh** in Power BI. Names and columns are stable. `export_manifest.csv` shows row counts and timestamps.

## 2. Data model (all relationships one-to-many, single direction, dimension -> fact)
| From (one) | To (many) |
|---|---|
| `dim_date[date_key]` | `fact_sales[date_key]` |
| `dim_customer[customer_id]` | `fact_sales[customer_id]`, `fact_segment_membership[customer_id]`, `fact_notification_log[customer_id]`, `fact_experiment_outcome[customer_id]`, `fact_cause_segment[customer_id]`, `fact_demo_purchases[customer_id]`, `fact_offer_proposal[customer_id]` |
| `dim_product[stock_code]` | `fact_sales[stock_code]`, `fact_demo_purchases[stock_code]`, `dim_offer[stock_code]` |
| `dim_segment[segment_id]` | `fact_segment_membership[segment_id]` |
| `dim_campaign[campaign_id]` | `fact_notification_log[campaign_id]` |
| `dim_experiment[experiment_id]` | `fact_experiment_outcome[experiment_id]`, `fact_cause_segment[experiment_id]` |

Standalone tables (no relationships): `experiment_summary`, `uplift_quintiles`, `association_rules`, `segment_summary`,
`quality_summary`, `missing_values_report`, `invalid_values_report`, `data_cleaning_log`, `processing_runs`, `export_manifest`.
Make `dim_customer[customer_id]` and `dim_product[stock_code]` text columns so keys match.

## 3. DAX measures
Create a table `_Measures` (Enter data, one blank column) and add:
```DAX
Total Sales = SUM(fact_sales[line_total])
Transactions = DISTINCTCOUNT(fact_sales[invoice])
Unique Customers = DISTINCTCOUNT(fact_sales[customer_id])
Products Sold = DISTINCTCOUNT(fact_sales[stock_code])
Avg Transaction Value = DIVIDE([Total Sales], [Transactions])
Sales Prior Month = CALCULATE([Total Sales], DATEADD(dim_date[date], -1, MONTH))
Sales MoM % = DIVIDE([Total Sales] - [Sales Prior Month], [Sales Prior Month])
Repeat Customers = COUNTROWS(FILTER(VALUES(dim_customer[customer_id]), CALCULATE(DISTINCTCOUNT(fact_sales[invoice])) > 1))
Repeat Rate = DIVIDE([Repeat Customers], [Unique Customers])
Customers in Segment = DISTINCTCOUNT(fact_segment_membership[customer_id])
Segment Spend = CALCULATE(SUM(dim_customer[monetary]), TREATAS(VALUES(fact_segment_membership[customer_id]), dim_customer[customer_id]))
Offers Generated = COUNTROWS(FILTER(dim_offer, dim_offer[origin] = "generated"))
Offers Configured = COUNTROWS(FILTER(dim_offer, dim_offer[origin] = "configured"))
Campaigns Processed = DISTINCTCOUNT(fact_notification_log[campaign_id])
Notifications Simulated = CALCULATE(COUNTROWS(fact_notification_log), fact_notification_log[status] = "simulated")
Sent To Provider = CALCULATE(COUNTROWS(fact_notification_log), fact_notification_log[status] = "sent_to_provider")
Confirmed Delivered = CALCULATE(COUNTROWS(fact_notification_log), fact_notification_log[status] = "delivered")
Suppressed = CALCULATE(COUNTROWS(fact_notification_log), fact_notification_log[status] = "suppressed")
Skipped (no offer) = CALCULATE(COUNTROWS(fact_notification_log), fact_notification_log[status] = "skipped")
Failed = CALCULATE(COUNTROWS(fact_notification_log), fact_notification_log[status] = "failed")
Treatment N = CALCULATE(COUNTROWS(fact_experiment_outcome), fact_experiment_outcome[group_name] = "treatment")
Control N = CALCULATE(COUNTROWS(fact_experiment_outcome), fact_experiment_outcome[group_name] = "control")
Conv Treatment = DIVIDE(CALCULATE(SUM(fact_experiment_outcome[purchased]), fact_experiment_outcome[group_name] = "treatment"), [Treatment N])
Conv Control = DIVIDE(CALCULATE(SUM(fact_experiment_outcome[purchased]), fact_experiment_outcome[group_name] = "control"), [Control N])
Conv Difference = [Conv Treatment] - [Conv Control]
CI Low = MAX(experiment_summary[ci_low])
CI High = MAX(experiment_summary[ci_high])
P Value = MAX(experiment_summary[p_value])
Rows Before Cleaning = CALCULATE(MAX(quality_summary[before]), quality_summary[metric] = "rows")
Rows After Cleaning = CALCULATE(MAX(quality_summary[after]), quality_summary[metric] = "rows")
Latest Execution Mode = LOOKUPVALUE(processing_runs[execution_mode_label], processing_runs[run_id], MAX(processing_runs[run_id]))
```
`CI Low/High/P Value` are read from the table Python wrote (`experiment_summary`); do not recompute them in DAX.

## 4. Pages
Add a text box on every page: **data label** (real / synthetic / simulated, see `export_manifest[data_label]`).

**Page 1 Executive Retail Overview** - Cards: Total Sales, Transactions, Unique Customers, Products Sold, Avg Transaction Value. Line chart:
`dim_date[year_month]` x Total Sales. Bar: top 10 `dim_product[description]` by Total Sales (Top N filter). Bar: `dim_product[category]` by
Total Sales (note: categories are keyword-DERIVED labels; "Other / Uncertain" is shown, not hidden). Slicers: `dim_date[year]`, `dim_date[month_name]`,
`dim_product[category]`, `fact_sales[country]`.

**Page 2 Customer Analytics** - Scatter: `dim_customer[frequency]` x `monetary`, legend `rfm_segment`. Column: customers per `rfm_segment`; per
`kmeans_cluster`. Histogram-style column: spend bands of `monetary`. Cards: Repeat Customers, Repeat Rate. Table (customer-level details): customer_id,
recency_days, frequency, monetary, top_category, top_product. Filter `dim_customer[data_label]` = real. Add a **drill-through** page `Customer Detail`
(field: `dim_customer[customer_id]`) showing the customer's features, segment memberships with `reason`, and notification log.

**Page 3 Product-Preference Segmentation** - Bar: `dim_segment[segment_name]` x Customers in Segment, filtered `segment_type` in (category, product).
Matrix: category segments x Segment Spend. Table: `fact_offer_proposal` (customer, product, status, reason) and `dim_offer`. Bar: customers eligible
per offer (count of proposals by offer_code). Table: `association_rules` (antecedent, consequent, lift). Slicer: `segment_type`. Cross-filtering lets you click a
segment to see its customers and spend.

**Page 4 Campaign Response and Cause-Based Segmentation** - Banner (text box, red): **"SIMULATED experiment: control = real observed purchases; treatment
response is simulated. Labels with evidence = insufficient are provisional."** Cards: Treatment N, Control N, Conv Treatment, Conv Control, Conv Difference,
CI Low, CI High, P Value. Column chart: counts of `fact_cause_segment[segment_label]` with legend `evidence_level` (shows Persuadable / Sure Thing / Lost
Cause / Do-not-disturb). Table: `uplift_quintiles` (check whether observed uplift rises with predicted uplift).

**Page 5 Offer and Notification Performance** - Cards: Offers Configured, Offers Generated, Campaigns Processed, Notifications Simulated, Sent To Provider,
Confirmed Delivered, Suppressed, Skipped (no offer), Failed. Bar: status by `fact_notification_log[department]`. Bar: top
`skip_or_suppression_reason` values. Table (audit log): attempted_at, recipient_name, department, status, delivery_mode, matched_offers, selection_reason,
skip_or_suppression_reason, message_text. Slicers: `dim_campaign[name]`, `fact_notification_log[delivery_mode]`, `status`. "Sent To Provider" is not "delivered";
only "Confirmed Delivered" (provider-confirmed) is.

**Page 6 Data Quality and Big Data Processing** - Cards: Rows Before Cleaning, Rows After Cleaning, Latest Execution Mode. Table: `quality_summary`. Table:
`missing_values_report` (before vs after). Bar: `invalid_values_report[rule]` x rows. Table: `data_cleaning_log` (step, rows_before, rows_after). Table:
`processing_runs` (stage, execution_mode_label, started_at, duration_seconds, rows_in, rows_out, status). Note: Mode A rows mean Spark local[*] reading HDFS on one
machine, not a multi-node cluster.

## 5. Demo setup checklist
Windows PC with Power BI Desktop; folder copied; parameter set; all tables loaded; relationships as in section 2; measures created; six pages built; save as
`Incrementum.pbix` yourself. Test **Refresh** once before the demo.
