#!/bin/bash
# AIP/NOTAM 可視化システム - Webサーバー起動スクリプト (Linux/Ubuntu)

# スクリプトがあるディレクトリをカレントディレクトリに設定
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# 仮想環境のアクティベート（存在する場合）
if [ -d "venv" ]; then
    source venv/bin/activate
fi

echo "=== AIP/NOTAM 可視化システム Webサーバー起動中 ==="
echo "Python環境: $(which python3)"

# 環境変数の初期値設定 (ポート・ホスト等)
export PORT="${PORT:-8000}"
export HOST="${HOST:-0.0.0.0}"

# サーバーIPの取得
SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "バックエンド (uvicorn): http://${HOST}:${PORT}"
echo ""
echo "🔒 HTTPS アクセス (Nginx経由):"
echo "   https://${SERVER_IP}"
echo ""
echo "⚠️ 自己署名証明書のため、ブラウザで警告が出ます。"
echo "   「詳細設定」→「アクセスする」を選択してください。"
echo ""

# Uvicornサーバー起動 (venvのPythonを優先使用)
if [ -f "$SCRIPT_DIR/venv/bin/python" ]; then
    exec "$SCRIPT_DIR/venv/bin/python" main.py web
else
    exec python3 main.py web
fi

