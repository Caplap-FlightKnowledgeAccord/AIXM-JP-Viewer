"""
Linux (Ubuntu) クラウドサーバー用パッケージング処理

本スクリプトは、AIPの元データ（大容量AIXM XML）を除外し、
事前にローカルで構築した AIP DuckDB (aip.duckdb) および NOTAM 処理・Web配信機能一式を
Ubuntuサーバー配布用パッケージ (.tar.gz) にまとめます。
"""

import os
import sys
import shutil
import tarfile
from pathlib import Path

# プロジェクトルートのパス取得
BASE_DIR = Path(__file__).resolve().parent.parent

# 出力先設定
DIST_DIR = BASE_DIR / "dist"
PACKAGE_NAME = "aixm-jp-viewer-server"
PACKAGE_DIR = DIST_DIR / PACKAGE_NAME
ARCHIVE_PATH = DIST_DIR / f"{PACKAGE_NAME}.tar.gz"

# 同梱・除外設定
AIP_DB_PATH = BASE_DIR / "data" / "processed" / "duckdb" / "aip.duckdb"
NOTAM_DB_PATH = BASE_DIR / "data" / "processed" / "duckdb" / "notam.duckdb"
TEMPLATES_DIR = BASE_DIR / "scripts" / "templates"


def verify_prerequisites():
    """パッケージングに必要なファイルの存在確認"""
    print("=== [1/5] 前提条件チェック ===")
    if not AIP_DB_PATH.exists():
        print(f"[ERROR] AIP実運用データベースが見つかりません: {AIP_DB_PATH}")
        print("        事前に `python main.py build-aip` を実行して aip.duckdb を生成してください。")
        sys.exit(1)
    
    size_mb = AIP_DB_PATH.stat().st_size / (1024 * 1024)
    print(f"[OK] AIPデータベース確認: {AIP_DB_PATH.name} ({size_mb:.2f} MB)")

    if NOTAM_DB_PATH.exists():
        notam_mb = NOTAM_DB_PATH.stat().st_size / (1024 * 1024)
        print(f"[OK] NOTAMデータベース確認: {NOTAM_DB_PATH.name} ({notam_mb:.2f} MB)")
    else:
        print(f"[INFO] NOTAMデータベースは未作成です（サーバー側で初期化されます）")


def clean_build_dir():
    """古いパッケージのクリーンアップ"""
    print("\n=== [2/5] パッケージ出力ディレクトリのクリーンアップ ===")
    if PACKAGE_DIR.exists():
        shutil.rmtree(PACKAGE_DIR)
        print(f"・古いパッケージフォルダを削除しました: {PACKAGE_DIR}")
    if ARCHIVE_PATH.exists():
        ARCHIVE_PATH.unlink()
        print(f"・古いアーカイブファイルを削除しました: {ARCHIVE_PATH.name}")

    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"・パッケージフォルダを作成しました: {PACKAGE_DIR}")


def copy_source_code():
    """ソースコードと設定ファイルのコピー"""
    print("\n=== [3/5] ソースコードと設定ファイルのコピー ===")
    
    # 必須ルートファイル
    root_files = ["main.py", "requirements.txt", "README.md", "config"]
    for item_name in root_files:
        src_path = BASE_DIR / item_name
        dest_path = PACKAGE_DIR / item_name
        if src_path.is_file():
            shutil.copy2(src_path, dest_path)
            print(f"  + ファイル: {item_name}")
        elif src_path.is_dir():
            shutil.copytree(
                src_path,
                dest_path,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
            )
            print(f"  + ディレクトリ: {item_name}/")

    # src ディレクトリのコピー (pycache等の除外)
    src_dir = BASE_DIR / "src"
    if src_dir.exists():
        shutil.copytree(
            src_dir,
            PACKAGE_DIR / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
        )
        print(f"  + ディレクトリ: src/")


def copy_data_files():
    """事前ビルドデータおよび必要なディレクトリ構造の構築 (AIP元データは除外)"""
    print("\n=== [4/5] データ構造と事前ビルドDBの配置 (AIXM元データは除外) ===")

    # ディレクトリ作成
    duckdb_dir = PACKAGE_DIR / "data" / "processed" / "duckdb"
    raw_notam_dir = PACKAGE_DIR / "data" / "raw" / "notam"
    logs_dir = PACKAGE_DIR / "logs"

    duckdb_dir.mkdir(parents=True, exist_ok=True)
    raw_notam_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # empty keep ファイル
    (raw_notam_dir / ".gitkeep").touch()
    (logs_dir / ".gitkeep").touch()

    # aip.duckdb のコピー
    dest_aip_db = duckdb_dir / "aip.duckdb"
    shutil.copy2(AIP_DB_PATH, dest_aip_db)
    print(f"  + AIP DBコピー: {dest_aip_db.relative_to(PACKAGE_DIR)} ({dest_aip_db.stat().st_size / (1024*1024):.2f} MB)")

    # notam.duckdb のコピー (存在時)
    if NOTAM_DB_PATH.exists():
        dest_notam_db = duckdb_dir / "notam.duckdb"
        shutil.copy2(NOTAM_DB_PATH, dest_notam_db)
        print(f"  + NOTAM DBコピー: {dest_notam_db.relative_to(PACKAGE_DIR)} ({dest_notam_db.stat().st_size / (1024*1024):.2f} MB)")

    print("  [EXCLUDED] AIP元データ (data/raw/aip/) およびアーカイブDB (aip_archive*.duckdb) を除外しました")


def copy_deployment_helpers():
    """Ubuntuデプロイ用スクリプトの配置"""
    print("\n=== [5/5] Ubuntuデプロイ用ヘルパースクリプトの配置 ===")
    if not TEMPLATES_DIR.exists():
        print(f"[WARN] テンプレートフォルダが見つかりません: {TEMPLATES_DIR}")
        return

    templates = {
        "setup_ubuntu.sh": "setup_ubuntu.sh",
        "start_server.sh": "start_server.sh",
        "aixm-viewer.service": "aixm-viewer.service",
        "nginx-aixm-viewer.conf": "nginx-aixm-viewer.conf",
        "README_DEPLOY.md": "README_DEPLOY.md",
    }

    for src_name, dest_name in templates.items():
        src_path = TEMPLATES_DIR / src_name
        dest_path = PACKAGE_DIR / dest_name
        if src_path.exists():
            shutil.copy2(src_path, dest_path)
            print(f"  + デプロイヘルパー: {dest_name}")


def create_tar_gz():
    """tar.gz アーカイブの作成"""
    print(f"\n=== .tar.gz アーカイブの生成: {ARCHIVE_PATH.name} ===")
    with tarfile.open(ARCHIVE_PATH, "w:gz") as tar:
        tar.add(PACKAGE_DIR, arcname=PACKAGE_NAME)

    size_mb = ARCHIVE_PATH.stat().st_size / (1024 * 1024)
    print(f"[OK] パッケージング完了! アーカイブサイズ: {size_mb:.2f} MB")
    print(f"     出力場所: {ARCHIVE_PATH}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("==================================================")
    print(" AIP/NOTAM Viewer - Linux (Ubuntu) パッケージ作成 ")
    print("==================================================")
    verify_prerequisites()
    clean_build_dir()
    copy_source_code()
    copy_data_files()
    copy_deployment_helpers()
    create_tar_gz()
    print("\n[SUCCESS] デプロイパッケージの作成が正常に終了しました。")


if __name__ == "__main__":
    main()
