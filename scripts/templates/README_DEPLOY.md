# AIP/NOTAM 可視化システム - Ubuntuデプロイマニュアル

本パッケージは、Linux (Ubuntu) クラウドサーバー上で AIP/NOTAM 可視化システムを稼働させるためのデプロイメント用パッケージです。
事前ビルド済みの AIP データベース (`data/processed/duckdb/aip.duckdb`) が含まれており、大容量の AIP 元データ（AIXM XML）なしで動作します。

---

## 1. サーバーへのアップロード・解凍

ローカルPCで生成された `aixm-jp-viewer-server.tar.gz` を SCP または SFTP 等で Ubuntu サーバーにアップロードします。

```bash
# サーバー側でアーカイブを解凍
tar -zxvf aixm-jp-viewer-server.tar.gz
cd aixm-jp-viewer-server
```

---

## 2. 自動環境構築スクリプトの実行

セットアップスクリプトを実行し、Python 依存関係、Nginx + HTTPS（自己署名証明書）を自動で構築します。

```bash
chmod +x setup_ubuntu.sh start_server.sh
./setup_ubuntu.sh
```

このスクリプトは以下を自動で行います：
- Python 3 + venv + 依存ライブラリのインストール
- **Nginx のインストールと設定**
- **自己署名SSL証明書の生成（有効期限: 10年）**
- HTTPS リバースプロキシの設定（Nginx → uvicorn port 8000）
- HTTP → HTTPS の自動リダイレクト

---

## 3. Webサーバーの起動

### 3.1 手動起動 (テスト用)

```bash
./start_server.sh
```

アクセスURL:
- **HTTPS**: `https://<サーバーIP>` (Nginx経由 — **推奨**)
- HTTP (内部): `http://<サーバーIP>:8000` (uvicorn直接)

> ⚠️ **自己署名証明書に関する注意**
>
> 初回アクセス時にブラウザで「この接続ではプライバシーが保護されません」等の警告が表示されます。
> これは自己署名証明書の性質上正常な動作です。
> 「詳細設定」→「<サーバーIP>にアクセスする（安全ではありません）」を選択してください。
> 通信自体はSSL/TLSで暗号化されています。

---

### 3.2 Systemd サービスによる常駐化 (推奨)

サーバー起動時に自動で常駐プロセスとして実行する場合は、Systemd サービスを設定します。

```bash
# 1. アプリケーションディレクトリを /opt へ移動 (例)
sudo cp -r . /opt/aixm-jp-viewer
cd /opt/aixm-jp-viewer

# 2. Systemd サービスファイルを配置
sudo cp aixm-viewer.service /etc/systemd/system/

# 3. 実行ユーザーやディレクトリの設定調整（必要に応じて）
# sudo nano /etc/systemd/system/aixm-viewer.service

# 4. サービスの有効化と起動
sudo systemctl daemon-reload
sudo systemctl enable aixm-viewer
sudo systemctl start aixm-viewer

# 5. ステータス確認
sudo systemctl status aixm-viewer
```

> Nginx は既に `setup_ubuntu.sh` で systemd に登録・起動されています。
> アプリサーバー (uvicorn) と Nginx の両方が起動していれば、 `https://<サーバーIP>` でアクセスできます。

---

## 4. HTTPS 関連の運用

### SSL証明書の確認

```bash
# 証明書の内容・有効期限を確認
openssl x509 -in /etc/ssl/certs/aixm-viewer-selfsigned.crt -text -noout | grep -A2 "Validity"
```

### Nginx の管理コマンド

```bash
# 設定テスト
sudo nginx -t

# 再起動
sudo systemctl restart nginx

# ログ確認
sudo tail -f /var/log/nginx/error.log
```

### 将来的にドメイン + Let's Encrypt に移行する場合

独自ドメインを取得した場合は、Let's Encrypt で無料の正規SSL証明書を取得できます。

```bash
# certbot のインストール
sudo apt-get install -y certbot python3-certbot-nginx

# SSL証明書の取得と Nginx への自動設定
sudo certbot --nginx -d your-domain.example.com

# 自動更新の確認
sudo certbot renew --dry-run
```

---

## 5. NOTAM データの自動処理・リアルタイム受信

サーバー上で NOTAM ファイルの自動監視・DB反映や SWIM 受信を有効化する場合：

```bash
# NOTAM 解析・DB手動更新
python3 main.py build-notam

# NOTAM ファイル監視モード起動 (Phase 3)
python3 main.py watch

# SWIM AMQP 受信モード起動 (Phase 8)
python3 main.py swim
```

---

## ネットワーク構成図

```
[クライアント (ブラウザ)]
        │
        │ HTTPS (port 443)
        ▼
[Nginx リバースプロキシ]  ← SSL終端 (自己署名証明書)
        │                    HTTP→HTTPS 自動リダイレクト (port 80→443)
        │ HTTP (port 8000, localhost のみ)
        ▼
[uvicorn + FastAPI]       ← アプリケーションサーバー
        │
        ▼
[DuckDB (aip.duckdb / notam.duckdb)]
```
