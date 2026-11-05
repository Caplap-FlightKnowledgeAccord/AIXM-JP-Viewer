"""
全履歴アーカイブDB構築モジュール（仕様書 2.1, 2.2, 3.1, 3.2, 4.1 完全対応版）

- Pass 0: lxml.etree.iterparse による800MB級XMLのストリーミング分解
- Pass 1: 全Featureの要素抽出一括登録 (gml_id, feature_type, name, valid_from/valid_to, geojson)
- Pass 2: 汎用 xlink:href 解析 & parent_id 結合
- 全履歴保持: 物理削除なし (仕様書 3.3)
"""

import os
import sys
import glob
import duckdb
from pathlib import Path
from datetime import datetime, timezone

from config.settings import AIP_ARCHIVE_DB_PATH, RAW_AIP_SPLIT_DIR, RAW_AIP_DIR
from src.parsers.aixm_aip_parser import AIXMAipParser

# Windows cp932対策
sys.stdout.reconfigure(encoding='utf-8')


def create_archive_schema(con: duckdb.DuckDBPyConnection):
    """
    アーカイブDBスキーマ作成 (仕様書 4.1)。
    全履歴を保持するため (gml_id, valid_from) を複合主キーとする。
    """
    con.execute("""
        CREATE TABLE IF NOT EXISTS aip_features (
            gml_id VARCHAR NOT NULL,
            feature_type VARCHAR,
            name VARCHAR,
            geojson VARCHAR,
            parent_id VARCHAR,
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            properties_json VARCHAR,
            interpretation VARCHAR,
            sequence_number INTEGER,
            correction_number INTEGER,
            raw_xml VARCHAR,
            source_file VARCHAR,
            source_timestamp TIMESTAMP,
            PRIMARY KEY (gml_id, valid_from)
        )
    """)

    # インデックス作成 (仕様書 4.1)
    con.execute("CREATE INDEX IF NOT EXISTS idx_archive_feature_type ON aip_features(feature_type);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_archive_name ON aip_features(name);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_archive_parent_id ON aip_features(parent_id);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_archive_valid_from ON aip_features(valid_from);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_archive_valid_to ON aip_features(valid_to);")


def build_aip_archive():
    """
    全履歴アーカイブDB (aip_archive.duckdb) の新規作成・構築。
    仕様書 2.1 / 2.2 / 3.1 / 3.2 / 3.3 準拠。
    """
    print(f"🚀 全履歴アーカイブDB構築開始: {AIP_ARCHIVE_DB_PATH}")

    # 既存DB削除
    if os.path.exists(AIP_ARCHIVE_DB_PATH):
        try:
            os.remove(AIP_ARCHIVE_DB_PATH)
            print("  既存のアーカイブDBを削除しました。")
        except Exception as e:
            print(f"⚠️ アーカイブDB削除エラー: {e}")

    os.makedirs(os.path.dirname(AIP_ARCHIVE_DB_PATH), exist_ok=True)
    con = duckdb.connect(str(AIP_ARCHIVE_DB_PATH))

    # スキーマ作成 (仕様書 4.1)
    create_archive_schema(con)

    # ============================================================
    # Pass 0: 入力XMLの特定 & ストリーミング分割
    # ============================================================
    split_files = glob.glob(os.path.join(RAW_AIP_SPLIT_DIR, "*.xml"))
    if not split_files:
        xml_files = glob.glob(os.path.join(RAW_AIP_DIR, "AIXM_ALL_*.xml"))
        if not xml_files:
            xml_files = glob.glob(os.path.join(RAW_AIP_DIR, "*.xml"))
        if xml_files:
            target_xml = xml_files[0]
            print(f"  分割済みXMLが見つからないため分割を実行します: {target_xml}")
            AIXMAipParser.split_features(target_xml)
            split_files = glob.glob(os.path.join(RAW_AIP_SPLIT_DIR, "*.xml"))
        else:
            print("❌ RAW_AIP_DIRにAIXM XMLファイルが見つかりません。")
            con.close()
            return

    total_inserted = 0
    source_timestamp = datetime.now(timezone.utc).replace(tzinfo=None)

    # ============================================================
    # Pass 1 & Pass 2: 全Featureの要素抽出一括登録 & parent_id結合
    # ============================================================
    for file_path in split_files:
        feature_type = Path(file_path).stem
        print(f"  📦 解析中: {feature_type} ({file_path})")

        records = AIXMAipParser.parse_feature_file(file_path, feature_type)
        if not records:
            print(f"    -> 対象レコードなし")
            continue

        # バッチインサート用データ準備
        rows = []
        for rec in records:
            gml_id = rec['gml_id']
            valid_from = rec['valid_from']
            if not gml_id or valid_from is None:
                continue

            rows.append((
                gml_id,
                rec['feature_type'],
                rec['name'],
                rec['geojson'],
                rec['parent_id'] or None,
                valid_from,
                rec['valid_to'],
                rec['properties_json'],
                rec['interpretation'],
                rec['sequence_number'],
                rec['correction_number'],
                rec['raw_xml'],
                Path(file_path).name,
                source_timestamp,
            ))

        if rows:
            con.executemany("""
                INSERT OR REPLACE INTO aip_features (
                    gml_id, feature_type, name, geojson, parent_id,
                    valid_from, valid_to, properties_json,
                    interpretation, sequence_number, correction_number,
                    raw_xml, source_file, source_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)

            inserted_count = len(rows)
            total_inserted += inserted_count
            print(f"    -> {inserted_count} 件登録")

    # タイムスライス valid_to 補正 (同一 gml_id 内の過去タイムスライスの valid_to を次タイムスライスの valid_from で締め切る)
    print("  🔧 タイムスライスの valid_to 自動補正中...")
    con.execute("""
        UPDATE aip_features f1
        SET valid_to = (
            SELECT MIN(f2.valid_from)
            FROM aip_features f2
            WHERE f2.gml_id = f1.gml_id
              AND f2.valid_from > f1.valid_from
        )
        WHERE f1.valid_to IS NULL
          AND EXISTS (
            SELECT 1 FROM aip_features f3
            WHERE f3.gml_id = f1.gml_id
              AND f3.valid_from > f1.valid_from
          );
    """)

    # PERMDELTAタイムスライスの name / parent_id 継承補正 (BASELINE / 過去タイムスライスから引き継ぐ)
    print("  🔧 タイムスライスの name / parent_id 自動継承中...")
    con.execute("""
        UPDATE aip_features f
        SET 
            name = filled.filled_name,
            parent_id = filled.filled_parent
        FROM (
            SELECT 
                gml_id,
                valid_from,
                LAST_VALUE(NULLIF(name, '') IGNORE NULLS) OVER (
                    PARTITION BY gml_id ORDER BY valid_from, sequence_number, correction_number
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) as filled_name,
                LAST_VALUE(NULLIF(parent_id, '') IGNORE NULLS) OVER (
                    PARTITION BY gml_id ORDER BY valid_from, sequence_number, correction_number
                    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                ) as filled_parent
            FROM aip_features
        ) filled
        WHERE f.gml_id = filled.gml_id AND f.valid_from = filled.valid_from;
    """)

    # コミット
    con.commit()

    count = con.execute("SELECT COUNT(*) FROM aip_features").fetchone()[0]
    con.close()
    print(f"✅ 全履歴アーカイブDB構築完了: 計 {count} 件のFeatureを保存しました。")



if __name__ == "__main__":
    build_aip_archive()