"""
AIP差分データ (AIXM Diff XML) 適用モジュール（仕様書 2.1, 3.3, 3.4, 4.1 完全対応版）

AIXM 5.1 の Temporality Model (時間モデル) に基づく DIFF 更新仕様:

- INSERT: 新しい gml_id をアーカイブDBに追加
- UPDATE: 対象 gml_id の属性、幾何情報、または有効開始日時 (valid_from) を更新
- WITHDRAWAL: 対象 gml_id の有効終了日時 (valid_to) に廃止発効日時を設定（物理削除なし）

適用後、実運用DB (aip.duckdb) を再構築する (仕様書 3.4)。
"""

import os
import glob
from pathlib import Path
from datetime import datetime, timezone
from config.settings import AIP_ARCHIVE_DB_PATH, RAW_AIP_DIR
import duckdb

ARCHIVE_DB_PATH = str(AIP_ARCHIVE_DB_PATH)

from src.parsers.aixm_diff_parser import AIXMDiffParser


def get_db_connection():
    """アーカイブDuckDB接続を取得"""
    con = duckdb.connect(ARCHIVE_DB_PATH)
    return con


def apply_aip_diffs():
    """data/raw/aip/ 内のすべての AIXM_DIFF_*.xml を順次適用する"""
    diff_pattern = str(RAW_AIP_DIR / "AIXM_DIFF_*.xml")
    diff_files = sorted(glob.glob(diff_pattern))

    if not diff_files:
        print(f"適用すべきAIP差分ファイルが見つかりません ({diff_pattern})")
        return

    print(f"{len(diff_files)}件のAIXM差分ファイルが見つかりました。順次適用します...")
    print("=" * 80)

    conn = get_db_connection()

    try:
        for xml_path in diff_files:
            print(f"\n処理中: {Path(xml_path).name}")
            print("-" * 80)

            diff_data = AIXMDiffParser.parse_diff_file(xml_path)

            if not diff_data:
                print("  スキップ: パース結果が空です")
                continue

            # 新規 Feature (INSERT) と 既存 Feature 更新 (UPDATE) を分離
            raw_updates = diff_data.get('update', [])
            inserts = list(diff_data.get('insert', []))
            updates = []

            for rec in raw_updates:
                gml_id = rec.get('gml_id', '')
                if not gml_id:
                    continue
                cnt = conn.execute("SELECT COUNT(*) FROM aip_features WHERE gml_id = ?", [gml_id]).fetchone()[0]
                if cnt == 0:
                    inserts.append(rec)
                else:
                    updates.append(rec)

            conn.execute("BEGIN TRANSACTION;")

            try:
                # 廃止処理 (valid_toに廃止発効日時をセット = 物理削除なし) (仕様書 3.3)
                withdrawal_count = apply_withdrawals(conn, diff_data['withdrawal'])
                print(f"  [WITHDRAWAL] {withdrawal_count} 件処理")

                # 新規追加 (仕様書 3.3 INSERT)
                insert_count = apply_inserts(conn, inserts)
                print(f"  [INSERT] {insert_count} 件挿入")

                # 属性・幾何変更 (仕様書 3.3 UPDATE)
                update_count = apply_updates(conn, updates)
                print(f"  [UPDATE] {update_count} 件更新")

                conn.execute("COMMIT;")
                print(f"  ✅ 適用完了")

            except Exception as e:
                conn.execute("ROLLBACK;")
                print(f"  ❌ エラーによりロールバック: {e}")
                raise

    except Exception as e:
        print(f"\n❌ 処理中にエラーが発生しました: {e}")
        conn.close()
        raise

    finally:
        conn.close()

    print("\n" + "=" * 80)
    print("実運用DB (aip.duckdb) の再構築を開始...")
    try:
        from src.database.build_aip_current import build_aip_current
        build_aip_current()
        print("✅ 実運用DBの再構築完了")
    except Exception as e:
        print(f"❌ 実運用DBの再構築エラー: {e}")

    print("\n" + "=" * 80)
    print("✅ 全差分ファイルの適用が完了しました")


def apply_withdrawals(conn, withdrawals: list) -> int:
    """
    廃止 (WITHDRAWAL) 処理 (仕様書 3.3)。

    AIXM 5.1 に物理削除（DELETE）の概念は存在しないため、
    対象 gml_id の有効終了日時 (valid_to) に廃止発効日時を設定する。

    これにより、過去の履歴レコードはそのまま保持され、
    実運用DB抽出時に自動的に除外される。
    """
    if not withdrawals:
        return 0

    updated_count = 0

    for w in withdrawals:
        try:
            gml_id = w.get('gml_id', '')
            if not gml_id:
                continue

            valid_to = w.get('valid_to')
            if valid_to is None:
                # valid_to が取得できない場合は現在日時を使用
                valid_to = datetime.now(timezone.utc).replace(tzinfo=None)

            # 更新対象のレコード数を事前に確認
            target_count = conn.execute("""
                SELECT COUNT(*) FROM aip_features
                WHERE gml_id = ?
                  AND (valid_to IS NULL OR valid_to > ?)
                  AND valid_from <= ?
            """, [gml_id, valid_to, valid_to]).fetchone()[0]

            if target_count == 0:
                continue

            # 現行タイムスライス (有効期間が valid_to を跨ぐもの) の valid_to を更新
            conn.execute("""
                UPDATE aip_features
                SET valid_to = ?
                WHERE gml_id = ?
                  AND (valid_to IS NULL OR valid_to > ?)
                  AND valid_from <= ?
            """, [valid_to, gml_id, valid_to, valid_to])

            updated_count += 1
        except Exception as e:
            print(f"    警告: 廃止更新エラー (gml_id={w.get('gml_id', '?')}): {e}")

    return updated_count


def apply_inserts(conn, records: list) -> int:
    """
    新規追加 (INSERT) 処理 (仕様書 3.3)。

    新しい gml_id をアーカイブDBに追加する。
    """
    if not records:
        return 0

    insert_count = 0

    for record in records:
        try:
            gml_id = record.get('gml_id', '')
            valid_from = record.get('valid_from')
            if not gml_id or valid_from is None:
                continue

            conn.execute("""
                INSERT OR REPLACE INTO aip_features (
                    gml_id, feature_type, name, geojson, parent_id,
                    valid_from, valid_to, properties_json,
                    interpretation, sequence_number, correction_number,
                    raw_xml, source_file, source_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                gml_id,
                record.get('feature_type', ''),
                record.get('name', ''),
                record.get('geojson'),
                record.get('parent_id') or None,
                valid_from,
                record.get('valid_to'),
                record.get('properties_json'),
                record.get('interpretation', 'BASELINE'),
                record.get('sequence_number', 0),
                record.get('correction_number', 0),
                record.get('raw_xml', ''),
                'DIFF',
                datetime.now(timezone.utc).replace(tzinfo=None),
            ])
            insert_count += 1

        except Exception as e:
            print(f"    警告: 挿入エラー (gml_id={record.get('gml_id', '?')}): {e}")

    return insert_count


def apply_updates(conn, records: list) -> int:
    """
    属性・幾何変更 (UPDATE) 処理 (仕様書 3.3)。

    対象 gml_id の属性、幾何情報、または有効開始日時 (valid_from) を更新する。

    同一 gml_id で新しい valid_from のタイムスライスが存在する場合は
    直前のタイムスライスの valid_to を新タイムスライスの valid_from で締め切り、
    (gml_id, valid_from) の複合主キーにより INSERT で履歴を保持する。
    """
    if not records:
        return 0

    update_count = 0

    for record in records:
        try:
            gml_id = record.get('gml_id', '')
            valid_from = record.get('valid_from')
            if not gml_id or valid_from is None:
                continue

            # 既存レコードの有無を確認 (同一 gml_id, valid_from)
            existing = conn.execute("""
                SELECT COUNT(*) FROM aip_features
                WHERE gml_id = ? AND valid_from = ?
            """, [gml_id, valid_from]).fetchone()[0]

            if existing > 0:
                # 既存タイムスライスの更新
                conn.execute("""
                    UPDATE aip_features
                    SET feature_type = ?,
                        name = ?,
                        geojson = ?,
                        parent_id = ?,
                        valid_to = ?,
                        properties_json = ?,
                        interpretation = ?,
                        sequence_number = ?,
                        correction_number = ?,
                        raw_xml = ?,
                        source_file = 'DIFF',
                        source_timestamp = ?
                    WHERE gml_id = ? AND valid_from = ?
                """, [
                    record.get('feature_type', ''),
                    record.get('name', ''),
                    record.get('geojson'),
                    record.get('parent_id') or None,
                    record.get('valid_to'),
                    record.get('properties_json'),
                    record.get('interpretation', 'BASELINE'),
                    record.get('sequence_number', 0),
                    record.get('correction_number', 0),
                    record.get('raw_xml', ''),
                    datetime.now(timezone.utc).replace(tzinfo=None),
                    gml_id,
                    valid_from,
                ])
            else:
                # 直前のタイムスライスの valid_to を新タイムスライスの valid_from で締め切る (タイムスライス重複防止)
                conn.execute("""
                    UPDATE aip_features
                    SET valid_to = ?
                    WHERE gml_id = ?
                      AND valid_from < ?
                      AND (valid_to IS NULL OR valid_to > ?)
                """, [valid_from, gml_id, valid_from, valid_from])

                # 新しいタイムスライスとして追加 (履歴保持)
                conn.execute("""
                    INSERT INTO aip_features (
                        gml_id, feature_type, name, geojson, parent_id,
                        valid_from, valid_to, properties_json,
                        interpretation, sequence_number, correction_number,
                        raw_xml, source_file, source_timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    gml_id,
                    record.get('feature_type', ''),
                    record.get('name', ''),
                    record.get('geojson'),
                    record.get('parent_id') or None,
                    valid_from,
                    record.get('valid_to'),
                    record.get('properties_json'),
                    record.get('interpretation', 'BASELINE'),
                    record.get('sequence_number', 0),
                    record.get('correction_number', 0),
                    record.get('raw_xml', ''),
                    'DIFF',
                    datetime.now(timezone.utc).replace(tzinfo=None),
                ])

            update_count += 1

        except Exception as e:
            print(f"    警告: 更新エラー (gml_id={record.get('gml_id', '?')}): {e}")

    return update_count


if __name__ == "__main__":
    apply_aip_diffs()