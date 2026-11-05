#!/bin/bash
# AIP/NOTAM 可視化システム - Ubuntuサーバー環境構築スクリプト
set -e

echo "=== AIP/NOTAM 可視化システム: Ubuntuセットアップ開始 ==="

# スクリプトがあるディレクトリをカレントディレクトリに設定
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR"

# システムパッケージの更新と必要なツールのインストール
echo "[1/6] OSパッケージの更新と必要なツールのインストール..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip build-essential liblxml-dev libgeos-dev

# 仮想環境の作成
echo "[2/6] Python仮想環境 (venv) の作成..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "✅ venv を作成しました"
else
    echo "ℹ️ 既存の venv を使用します"
fi

# 仮想環境のアクティベートと依存ライブラリのインストール
echo "[3/6] 依存Pythonライブラリのインストール..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 必要なディレクトリ構造の確認
echo "[4/6] ディレクトリ構造の確認・作成..."
mkdir -p data/raw/notam
mkdir -p data/processed/duckdb
mkdir -p logs

chmod +x start_server.sh

# --- HTTPS 設定 (Nginx + 自己署名証明書) ---
echo "[5/6] Nginx のインストールと HTTPS リバースプロキシの設定..."
sudo apt-get install -y nginx

# 自己署名SSL証明書の生成 (既存でなければ)
SSL_CERT="/etc/ssl/certs/aixm-viewer-selfsigned.crt"
SSL_KEY="/etc/ssl/private/aixm-viewer-selfsigned.key"

if [ ! -f "$SSL_CERT" ] || [ ! -f "$SSL_KEY" ]; then
    echo "🔐 自己署名SSL証明書を生成中..."
    sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$SSL_KEY" \
        -out "$SSL_CERT" \
        -subj "/C=JP/ST=Tokyo/L=Tokyo/O=AIXM-JP-Viewer/CN=$(hostname -I | awk '{print $1}')"
    echo "✅ SSL証明書を生成しました (有効期限: 10年)"
    echo "   証明書: $SSL_CERT"
    echo "   秘密鍵: $SSL_KEY"
else
    echo "ℹ️ 既存のSSL証明書を使用します"
fi

# Nginx設定ファイルの配置
echo "[6/6] Nginx設定の適用..."
sudo cp nginx-aixm-viewer.conf /etc/nginx/sites-available/aixm-viewer
sudo ln -sf /etc/nginx/sites-available/aixm-viewer /etc/nginx/sites-enabled/aixm-viewer

# デフォルトのNginxサイトを無効化
sudo rm -f /etc/nginx/sites-enabled/default

# Nginx設定のテスト
if sudo nginx -t; then
    echo "✅ Nginx設定テスト: OK"
    sudo systemctl restart nginx
    sudo systemctl enable nginx
    echo "✅ Nginx を起動・有効化しました"
else
    echo "❌ Nginx設定にエラーがあります。手動で確認してください。"
    exit 1
fi

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "起動方法:"
echo "  1. アプリサーバー起動: ./start_server.sh"
echo "  2. HTTPS でアクセス:   https://<サーバーIP>"
echo ""
echo "⚠️ 自己署名証明書のため、初回アクセス時にブラウザで警告が表示されます。"
echo "   「詳細設定」→「<サーバーIP>にアクセスする（安全ではありません）」を選択してください。"
echo ""
echo "Systemdサービス設定方法は README_DEPLOY.md を参照してください。"
