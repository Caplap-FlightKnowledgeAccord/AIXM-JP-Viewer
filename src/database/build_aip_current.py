"""
実運用DB (aip.duckdb) 構築モジュール（仕様書 2.1, 2.2, 3.4, 4.1 完全対応版）

全履歴アーカイブDB (aip_archive.duckdb) から現在・将来において有効な
Feature のみを抽出して軽量な実運用DBを構築する。
抽出条件: valid_to >= AIRAC発効日時 OR valid_to IS NULL
"""

import os
import sys
import duckdb
from datetime import datetime, timezone

from config.settings import AIP_ARCHIVE_DB_PATH, AIP_DB_PATH

# Windows cp932対策
sys.stdout.reconfigure(encoding='utf-8')


def create_current_schema(con: duckdb.DuckDBPyConnection):
    """
    実運用DBスキーマ作成 (仕様書 4.1)。
    Read-Only で FastAPI からスレッドセーフに参照される。

    実運用DBは現在および将来有効な Feature を保持するため、
    同一 gml_id でも有効期間の異なる複数レコード（タイムスライス）が
    存在し得る。そのため gml_id は主キーとせず、インデックスのみ作成する。
    """
    con.execute("""
        CREATE TABLE aip_features (
            gml_id VARCHAR,
            feature_type VARCHAR,
            name VARCHAR,
            geojson VARCHAR,
            parent_id VARCHAR,
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            properties_json VARCHAR
        )
    """)

    # インデックス作成 (仕様書 4.1)
    con.execute("CREATE INDEX idx_aip_gml_id ON aip_features(gml_id);")
    con.execute("CREATE INDEX idx_aip_feature_type ON aip_features(feature_type);")
    con.execute("CREATE INDEX idx_aip_name ON aip_features(name);")
    con.execute("CREATE INDEX idx_aip_parent_id ON aip_features(parent_id);")
    con.execute("CREATE INDEX idx_aip_valid_from ON aip_features(valid_from);")
    con.execute("CREATE INDEX idx_aip_valid_to ON aip_features(valid_to);")


def build_aip_current(target_time: datetime = None):
    """
    全履歴アーカイブDB (aip_archive.duckdb) から現在・将来において有効な
    Feature のみを抽出して実運用DB (aip.duckdb) を構築する。
    仕様書 2.2 / 3.4 準拠。

    抽出条件: valid_to >= target_time OR valid_to IS NULL

    Args:
        target_time: 抽出基準日時 (UTC)。None の場合は現在時刻。
    """
    print(f"🚀 実運用DB (aip.duckdb) ビルド開始...")

    if not os.path.exists(AIP_ARCHIVE_DB_PATH):
        print(f"❌ アーカイブDBが見つかりません: {AIP_ARCHIVE_DB_PATH}")
        return False

    if target_time is None:
        target_time = datetime.now(timezone.utc).replace(tzinfo=None)

    print(f"  抽出基準日時 (UTC): {target_time}")

    # 既存の aip.duckdb を削除
    if os.path.exists(AIP_DB_PATH):
        try:
            os.remove(AIP_DB_PATH)
            print(f"  既存の実運用DBを削除しました: {AIP_DB_PATH}")
        except Exception as e:
            print(f"⚠️ 実運用DB削除エラー: {e}")

    os.makedirs(os.path.dirname(AIP_DB_PATH), exist_ok=True)

    # 1. アーカイブDBに接続し、aip.duckdb へエクスポート/コピー
    arch_con = duckdb.connect(str(AIP_ARCHIVE_DB_PATH), read_only=True)
    curr_con = duckdb.connect(str(AIP_DB_PATH))

    # 実運用DBにテーブルを作成 (仕様書 4.1)
    create_current_schema(curr_con)

    # アーカイブDBをATTACHして有効データを転送
    curr_con.execute(f"ATTACH '{AIP_ARCHIVE_DB_PATH}' AS archive_db (READ_ONLY);")

    # 仕様書 2.2: valid_to >= AIRAC発効日時 OR valid_to IS NULL
    # 現在および将来有効な Feature を全タイムスライス保持する。
    # 同一 gml_id でも有効期間の異なる複数レコード（現在有効・未来有効）が
    # 存在し得るため、最新1件のみに絞り込まない。
    insert_query = """
        INSERT INTO aip_features
        SELECT
            gml_id,
            feature_type,
            name,
            geojson,
            parent_id,
            valid_from,
            valid_to,
            properties_json
        FROM archive_db.aip_features
        WHERE valid_to IS NULL OR valid_to >= ?
    """

    curr_con.execute(insert_query, [target_time])

    extracted_count = curr_con.execute("SELECT COUNT(*) FROM aip_features").fetchone()[0]

    curr_con.close()
    arch_con.close()

    size_mb = os.path.getsize(AIP_DB_PATH) / (1024 * 1024)
    print(f"✅ 実運用DB (aip.duckdb) ビルド完了!")
    print(f"   - 格納レコード数: {extracted_count} 件")
    print(f"   - ファイルサイズ: {size_mb:.2f} MB")

    return True


if __name__ == "__main__":
    build_aip_current()