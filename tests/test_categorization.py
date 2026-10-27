from src import categorization as cat, config
from .helpers import make_db


def test_classify_known_products():
    assert cat.classify("DETTOL ANTISEPTIC LIQUID 500ML")[0] == "Non-Food & Household"
    assert cat.classify("COCA-COLA 750ML")[0] == "Frozen & Snacks"
    assert cat.classify("BASMATI RICE 5KG")[0] == "Pantry & Dry Goods"
    assert cat.classify("FULL CREAM MILK 1L")[0] == "Fresh & Chilled"


def test_unclassifiable_and_conflicting_are_not_forced():
    dept, conf, review = cat.classify("PINK CHERRY LIGHTS")
    assert dept == config.UNCERTAIN and review == 1
    dept, conf, review = cat.classify("RICE MILK")   # pantry + fresh keywords conflict
    assert dept == config.UNCERTAIN and review == 1


def test_manual_correction_survives_reclassification():
    conn = make_db()
    cat.set_manual(conn, "LIZ500", "Pantry & Dry Goods")
    cat.apply_to_products(conn)
    row = conn.execute("SELECT department, category_source FROM products WHERE stock_code='LIZ500'").fetchone()
    assert row["department"] == "Pantry & Dry Goods" and row["category_source"] == "manual"


def test_seeded_demo_catalogue_all_classified():
    conn = make_db()
    assert conn.execute("SELECT COUNT(*) FROM products WHERE needs_review=1").fetchone()[0] == 0


def test_giftware_classifier_fixes_the_supermarket_mistakes_seen_on_real_data():
    g = cat.classify_giftware
    assert g("ASSORTED CHEESE FRIDGE MAGNETS")[0] == "Home Decor"            # not 'Fresh & Chilled'
    assert g("FAUX FUR CHOCOLATE THROW")[0] == "Home Decor"                  # not 'Frozen & Snacks'
    assert g("SET/2 RED RETROSPOT TEA TOWELS")[0] == "Kitchen & Dining"      # not 'Pantry'
    assert g("PINK CHERRY LIGHTS")[0] == "Lights & Candles"
    assert g("6 CHOCOLATE LOVE HEART T-LIGHTS")[0] == "Lights & Candles"
    assert g("ICE CREAM DESIGN GARDEN PARASOL")[0] == "Garden & Outdoor"
    assert g("BUNNY EGG GARLAND")[0] == "Seasonal & Celebrations"


def test_giftware_ties_and_unknowns_are_not_forced():
    dept, conf, review = cat.classify_giftware("SET OF TEA COFFEE SUGAR TINS PANTRY")   # kitchen vs storage tie
    assert dept == config.UNCERTAIN and review == 1
    assert cat.classify_giftware("SOMETHING COMPLETELY ODD")[2] == 1
    assert cat.classify_giftware("")[0] == config.UNCERTAIN


def test_kaggle_products_get_giftware_labels_and_demo_products_keep_supermarket_labels():
    conn = make_db()
    conn.execute("INSERT INTO products (stock_code, description, is_synthetic) VALUES ('K1','PINK CHERRY LIGHTS',0)")
    conn.commit()
    cat.apply_to_products(conn)
    assert conn.execute("SELECT department FROM products WHERE stock_code='K1'").fetchone()[0] == "Lights & Candles"
    assert conn.execute("SELECT department FROM products WHERE stock_code='DET500'").fetchone()[0] == "Non-Food & Household"


def test_seeding_the_sample_catalogue_does_not_reclassify_other_products():
    conn = make_db()
    conn.execute("INSERT INTO products (stock_code, description, department, is_synthetic) VALUES ('K9','ITEM 9','Home Decor',0)")
    conn.commit()
    from src import demo_data
    demo_data.seed(conn)                                                       # re-seeding must leave real products alone
    assert conn.execute("SELECT department FROM products WHERE stock_code='K9'").fetchone()[0] == "Home Decor"
