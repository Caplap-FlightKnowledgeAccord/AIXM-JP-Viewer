import os
import sys
import glob
import duckdb
from config.settings import NOTAM_DB_PATH, RAW_NOTAM_DIR
from src.parsers.aixm_notam_parser import AIXMNotamParser

# Windows cp932対策
sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = str(NOTAM_DB_PATH)

def build_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = duckdb.connect(DB_PATH)
    
    # 空間拡張のロード
    con.execute("INSTALL spatial; LOAD spatial;")

    # 古いテーブルが存在する場合は削除してスキーマ変更を確定させる
    con.execute("DROP TABLE IF EXISTS notams;")

    # テーブル初期化 (notam_id を PRIMARY KEY に指定)
    con.execute("""
        CREATE TABLE notams (
            notam_id VARCHAR PRIMARY KEY,
            fir VARCHAR,
            series VARCHAR,
            location VARCHAR,
            q_code VARCHAR,
            notam_type VARCHAR,
            ref_notam_id VARCHAR,
            status VARCHAR,
            precision_type VARCHAR,
            operational_status VARCHAR,
            effective_start TIMESTAMP,
            effective_end TIMESTAMP,
            text VARCHAR,
            geometry GEOMETRY,
            geojson VARCHAR,
            file_path VARCHAR,
            scenario VARCHAR,
            schedule VARCHAR,
            timesheets_json VARCHAR,
            associated_aip_gml_id VARCHAR,
            lower_limit_ft INTEGER,
            upper_limit_ft INTEGER
        )
    """)

    # 通常のNOTAMフォルダ (RJJJ配下) と test フォルダの両方を対象にする
    xml_files = glob.glob(str(RAW_NOTAM_DIR / "**" / "*.xml"), recursive=True)
    print(f"📦 パース対象XML数: {len(xml_files)} 件")

    parsed_records = []
    for f in xml_files:
        parsed = AIXMNotamParser.parse_file(f, default_series="A", default_fir="RJJJ")
        if parsed:
            parsed_records.append(parsed)

    # 1. データの挿入
    for r in parsed_records:
        con.execute("""
            INSERT INTO notams VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                TRY_CAST(? AS TIMESTAMP), TRY_CAST(? AS TIMESTAMP),
                ?, ST_GeomFromText(?), ST_AsGeoJSON(ST_GeomFromText(?)), ?, ?, ?, ?, ?, ?, ?
            ) ON CONFLICT(notam_id) DO UPDATE SET
                status = excluded.status,
                text = excluded.text,
                geometry = excluded.geometry,
                geojson = excluded.geojson,
                scenario = excluded.scenario,
                schedule = excluded.schedule,
                timesheets_json = excluded.timesheets_json,
                operational_status = excluded.operational_status,
                associated_aip_gml_id = excluded.associated_aip_gml_id,
                lower_limit_ft = excluded.lower_limit_ft,
                upper_limit_ft = excluded.upper_limit_ft
        """, (
            r['notam_id'], r['fir'], r['series'], r['location'], r['q_code'],
            r['notam_type'], r['ref_notam_id'], r['status'], r['precision_type'],
            r.get('operational_status', 'NORMAL'),
            r['effective_start'], r['effective_end'],
            r['text'], r['geometry_wkt'], r['geometry_wkt'], r['file_path'],
            r.get('scenario', ''), r.get('schedule', ''), r.get('timesheets_json'),
            r.get('associated_aip_gml_id'),
            r.get('lower_limit_ft'), r.get('upper_limit_ft')
        ))

    # 2. NOTAM CNL (取り消し) の反映処理
    # NOTAM C のレコードを探し、対象となる old NOTAM の status を 'CANCELED' に変更
    con.execute("""
        UPDATE notams 
        SET status = 'CANCELED'
        WHERE notam_id IN (
            SELECT ref_notam_id FROM notams WHERE notam_type = 'C' AND ref_notam_id IS NOT NULL
        )
    """)
    
    # CNL 自身も描画させないために 'CANCELED' 扱いにする
    con.execute("UPDATE notams SET status = 'CANCELED' WHERE notam_type = 'C'")

    count = con.execute("SELECT COUNT(*) FROM notams WHERE status = 'ACTIVE'").fetchone()[0]
    print(f"✅ DB更新完了: 有効(ACTIVE)なNOTAM件数 = {count} 件")
    
    # 場所別の件数確認
    loc_counts = con.execute("""
        SELECT location, COUNT(*) FROM notams WHERE status = 'ACTIVE' GROUP BY location ORDER BY COUNT(*) DESC
    """).fetchall()
    for loc, cnt in loc_counts:
        print(f"  {loc}: {cnt} 件")
    
    con.close()

if __name__ == "__main__":
    build_database()