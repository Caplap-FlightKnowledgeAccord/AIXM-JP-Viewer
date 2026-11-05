"""
AIXMからDBを構築するメインパイプライン（仕様書 2.1, 2.2, 3.1-3.4 準拠）

処理フロー:
1. Pass 0: lxml.etree.iterparse による800MB級XMLのストリーミング分割 (3.1)
2. Pass 1: 全Featureの要素抽出一括登録 (3.2)
3. Pass 2: 汎用 xlink:href 解析 & parent_id 結合 (3.2)
4. 全履歴アーカイブDB構築 (2.1) - 物理削除なし (3.3)
5. 実運用DB構築 (2.2, 3.4) - valid_toフィルター
"""

import os
import sys
import glob
from datetime import datetime

from config.settings import AIP_ARCHIVE_DB_PATH, AIP_DB_PATH, RAW_AIP_DIR
from src.parsers.aixm_aip_parser import AIXMAipParser

# Windows cp932対策
sys.stdout.reconfigure(encoding='utf-8')


def build_aip_database(target_time: datetime = None):
    """AIXM XMLから2DB構成 (アーカイブDB + 実運用DB) を一括構築する"""
    print("=" * 80)
    print("AIXMからDB構築パイプライン開始")
    print("=" * 80)

    # --- Pass 0: 入力XMLの特定 & ストリーミング分割 (仕様書 3.1) ---
    xml_files = sorted(glob.glob(os.path.join(RAW_AIP_DIR, "AIXM_ALL_*.xml")))
    if not xml_files:
        xml_files = sorted(glob.glob(os.path.join(RAW_AIP_DIR, "*.xml")))
        xml_files = [f for f in xml_files if "DIFF" not in os.path.basename(f)]

    if not xml_files:
        print(f"RAW_AIP_DIRにAIXM XMLファイルが見つかりません: {RAW_AIP_DIR}")
        return False

    target_xml = xml_files[0]
    print(f"処理対象XML: {target_xml}")

    print("-" * 80)
    print("Pass 0: ストリーミング分割 (仕様書 3.1)")
    feature_counts = AIXMAipParser.split_features(target_xml)
    print(f"分割結果: {feature_counts}")
    print("-" * 80)

    # --- Pass 1 & Pass 2: アーカイブDB構築 (仕様書 2.1, 3.2) ---
    print("-" * 80)
    print("Pass 1 & Pass 2: アーカイブDB構築")
    from src.database.build_aip_archive import build_aip_archive
    build_aip_archive()
    print("-" * 80)

    # --- 実運用DB (aip.duckdb) 構築 (仕様書 2.2, 3.4) ---
    print("-" * 80)
    print("実運用DB (aip.duckdb) 構築")
    from src.database.build_aip_current import build_aip_current
    build_aip_current(target_time)
    print("-" * 80)

    # --- 結果サマリー ---
    import duckdb

    arch_con = duckdb.connect(str(AIP_ARCHIVE_DB_PATH), read_only=True)
    archive_total = arch_con.execute("SELECT COUNT(*) FROM aip_features").fetchone()[0]
    arch_by_type = arch_con.execute(
        "SELECT feature_type, COUNT(*) FROM aip_features GROUP BY feature_type"
    ).fetchall()
    arch_con.close()

    curr_con = duckdb.connect(str(AIP_DB_PATH), read_only=True)
    current_total = curr_con.execute("SELECT COUNT(*) FROM aip_features").fetchone()[0]
    curr_by_type = curr_con.execute(
        "SELECT feature_type, COUNT(*) FROM aip_features GROUP BY feature_type"
    ).fetchall()
    curr_con.close()

    print("=" * 80)
    print("構築結果サマリー")
    print(f"  アーカイブDB: {AIP_ARCHIVE_DB_PATH}")
    print(f"    総レコード数: {archive_total:,} 件")
    print(f"    ファイルサイズ: {os.path.getsize(AIP_ARCHIVE_DB_PATH) / (1024 * 1024):.2f} MB")
    for ftype, cnt in arch_by_type[:10]:
        print(f"      - {ftype}: {cnt:,} 件")

    print(f"  実運用DB: {AIP_DB_PATH}")
    print(f"    総レコード数: {current_total:,} 件")
    print(f"    ファイルサイズ: {os.path.getsize(AIP_DB_PATH) / (1024 * 1024):.2f} MB")
    for ftype, cnt in curr_by_type[:10]:
        print(f"      - {ftype}: {cnt:,} 件")

    print("=" * 80)
    print("AIXMからDB構築パイプライン完了")
    return True


if __name__ == "__main__":
    build_aip_database()