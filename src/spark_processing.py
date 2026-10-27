"""MODE A - PySpark DataFrames + Spark SQL, reading the cleaned file from HDFS.

Honest labelling: Spark runs in LOCAL mode (master=local[*]) inside this one VM. It is real Spark and real HDFS,
but it is NOT a multi-node cluster. If HDFS is unreachable the script says so and reads the local file instead.
"""
import time

from . import config, database, hdfs_manager

QUERIES = {
    "sales_summary": """SELECT ROUND(SUM(line_total),2) AS total_sales, COUNT(DISTINCT invoice) AS transactions,
        COUNT(DISTINCT customer_id) AS customers, COUNT(DISTINCT stock_code) AS products,
        ROUND(SUM(line_total)/COUNT(DISTINCT invoice),2) AS avg_transaction_value FROM sales""",
    "monthly_sales": """SELECT year_month, ROUND(SUM(line_total),2) AS sales, COUNT(DISTINCT invoice) AS transactions,
        COUNT(DISTINCT customer_id) AS customers FROM sales GROUP BY year_month ORDER BY year_month""",
    "top_products": """SELECT stock_code, description, SUM(quantity) AS units, ROUND(SUM(line_total),2) AS revenue,
        COUNT(DISTINCT invoice) AS transactions FROM sales GROUP BY stock_code, description
        ORDER BY revenue DESC LIMIT 50""",
    "department_sales": """SELECT department, ROUND(SUM(line_total),2) AS sales, COUNT(DISTINCT stock_code) AS products,
        COUNT(DISTINCT customer_id) AS customers FROM sales GROUP BY department ORDER BY sales DESC""",
    "customer_rfm_base": """WITH inv AS (SELECT customer_id, invoice, to_date(MIN(invoice_date)) AS d,
            SUM(line_total) AS t, SUM(quantity) AS q FROM sales GROUP BY customer_id, invoice),
        ref AS (SELECT date_add(MAX(d),1) AS ref_date FROM inv)
        SELECT customer_id, CAST(MAX(d) AS STRING) AS last_purchase_date,
               datediff((SELECT ref_date FROM ref), MAX(d)) AS recency_days, COUNT(*) AS frequency,
               ROUND(SUM(t),2) AS monetary, ROUND(AVG(t),2) AS avg_basket_value, ROUND(AVG(q),2) AS avg_quantity
        FROM inv GROUP BY customer_id""",
}


def run_spark(conn=None, out_dir=None):
    from pyspark.sql import SparkSession
    from pyspark.sql.functions import col

    out_dir = out_dir or config.PROCESSED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or database.connect()
    lines = []

    def say(msg):
        stamp = f"[{time.strftime('%H:%M:%S')}] {msg}"
        lines.append(stamp)
        print(stamp)

    hdfs_ok = hdfs_manager.is_available()
    say("HDFS status: " + ("AVAILABLE - reading input from HDFS" if hdfs_ok
                           else "NOT reachable - HDFS is NOT being used; Spark will read the local file"))
    t0 = time.time()
    run_id = database.start_run(conn, "processing_spark", "A_hadoop_spark" if hdfs_ok else "B_local_python",
                                "Spark local[*] + HDFS" if hdfs_ok else "Spark local[*] on LOCAL file (HDFS unavailable)")
    try:
        spark = (SparkSession.builder.master("local[*]").appName("Incrementum")
                 .config("spark.driver.memory", "2g").config("spark.sql.shuffle.partitions", "8").getOrCreate())
        spark.sparkContext.setLogLevel("WARN")
        say(f"Spark {spark.version} started (master=local[*], single machine - not a cluster)")
        src = (f"{config.HDFS_URI}{config.HDFS_CLEANED}/cleaned_sales.csv" if hdfs_ok
               else f"file://{config.CLEANED_DIR / 'cleaned_sales.csv'}")
        df = spark.read.csv(src, header=True, inferSchema=True).withColumn("customer_id", col("customer_id").cast("string"))
        rows = df.count()
        say(f"spark.read.csv({src}) -> DataFrame with {rows:,} rows, {len(df.columns)} columns")
        df.createOrReplaceTempView("sales")
        say("createOrReplaceTempView('sales')")
        n_out = 0
        for name, sql in QUERIES.items():
            res = spark.sql(sql)
            pdf = res.toPandas()
            pdf.to_csv(out_dir / f"{name}.csv", index=False)
            n_out += len(pdf)
            say(f"spark.sql[{name}] -> {len(pdf):,} rows -> {out_dir / (name + '.csv')}")
            if hdfs_ok:
                target = f"{config.HDFS_URI}{config.HDFS_PROCESSED}/{name}"
                res.coalesce(1).write.mode("overwrite").option("header", True).csv(target)
                say(f"wrote {target} (HDFS)")
        spark.stop()
        say("Spark session stopped")
        database.finish_run(conn, run_id, t0, rows, n_out)
        (config.LOGS_DIR / "spark_run.log").write_text("\n".join(lines) + "\n")
        return {"hdfs_used": hdfs_ok, "rows": rows}
    except Exception as e:
        database.finish_run(conn, run_id, t0, status="failed", notes=str(e)[:300])
        raise
    finally:
        if own:
            conn.close()
