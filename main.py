"""
AIP/NOTAM 可視化システム - 統合エントリーポイント

使用方法:
  python main.py build-aip       # AIPデータの分割・解析・DB格納
  python main.py build-notam     # NOTAMデータの解析・DB格納
  python main.py build-all       # 全データの構築
  python main.py web             # Webサーバー起動
  python main.py watch           # NOTAMファイル監視モード
  python main.py swim            # SWIM AMQP受信モード
"""

import sys
import argparse


def build_aip():
    """AIPデータパイプライン: Pass0分割 → アーカイブDB構築 → 実運用DB構築 (仕様書 2.1, 3.1-3.4)"""
    print("=== AIPデータパイプライン開始 ===")
    from src.database.aip_db_builder import build_aip_database
    build_aip_database()


def build_notam():
    """NOTAMデータパイプライン: 解析 → DB格納"""
    print("=== NOTAMデータパイプライン開始 ===")
    from src.database.notam_db_builder import build_database
    build_database()


def build_diff():
    """AIP差分データ適用パイプライン"""
    print("=== AIP差分データ更新パイプライン開始 ===")
    from update_aip_diff import apply_aip_diffs
    apply_aip_diffs()


def build_all():
    """全データの構築"""
    build_aip()
    build_diff()
    build_notam()


def start_web():
    """Webサーバー起動"""
    import os
    print("=== Webサーバー起動 ===")
    from config.settings import WEB_HOST, WEB_PORT
    # 環境変数によるホスト・ポート上書き (クラウドデプロイ対応)
    host = os.getenv("HOST", WEB_HOST)
    port = int(os.getenv("PORT", str(WEB_PORT)))
    print(f"http://{host}:{port} で起動中...")
    import uvicorn
    from src.web.main import app
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )



def start_watch():
    """NOTAMファイル監視モード"""
    print("=== NOTAMファイル監視モード ===")
    print("⚠️ ファイル監視モードは未実装です (Phase 3で対応予定)")


def start_swim():
    """SWIM AMQP受信モード"""
    print("=== SWIM AMQP受信モード ===")
    print("⚠️ SWIM受信モードは未実装です (Phase 8で対応予定)")


def package_server():
    """Linuxサーバー用パッケージ作成"""
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("=== Linuxサーバー用パッケージ作成開始 ===")
    from scripts.package_server import main as run_package
    run_package()


def main():
    parser = argparse.ArgumentParser(
        description="AIP/NOTAM 可視化システム",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python main.py build-aip       AIPデータの分割・解析・DB格納
  python main.py build-notam     NOTAMデータの解析・DB格納
  python main.py build-all       全データの構築
  python main.py web             Webサーバー起動
  python main.py watch            NOTAMファイル監視モード
  python main.py swim             SWIM AMQP受信モード
  python main.py package          Linuxサーバー用パッケージ作成
        """
    )
    subparsers = parser.add_subparsers(dest="command", help="実行コマンド")

    subparsers.add_parser("build-aip", help="AIPデータの分割・解析・DB格納")
    subparsers.add_parser("build-diff", help="AIP差分データの適用・DB更新")
    subparsers.add_parser("build-notam", help="NOTAMデータの解析・DB格納")
    subparsers.add_parser("build-all", help="全データの構築")
    subparsers.add_parser("web", help="Webサーバー起動")
    subparsers.add_parser("watch", help="NOTAMファイル監視モード")
    subparsers.add_parser("swim", help="SWIM AMQP受信モード")
    subparsers.add_parser("package", help="Linuxサーバー用パッケージ作成")

    args = parser.parse_args()

    if args.command == "build-aip":
        build_aip()
    elif args.command == "build-diff":
        build_diff()
    elif args.command == "build-notam":
        build_notam()
    elif args.command == "build-all":
        build_all()
    elif args.command == "web":
        start_web()
    elif args.command == "watch":
        start_watch()
    elif args.command == "swim":
        start_swim()
    elif args.command == "package":
        package_server()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
