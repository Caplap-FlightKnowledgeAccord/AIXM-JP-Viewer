import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# プロジェクトルートディレクトリ
BASE_DIR = Path(__file__).resolve().parent.parent

# .env ファイル自動読み込み
def _load_env_file():
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except Exception:
            pass

_load_env_file()

# データディレクトリ設定
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
RAW_NOTAM_DIR = RAW_DIR / "notam"
RAW_AIP_DIR = RAW_DIR / "aip"
RAW_AIP_SPLIT_DIR = RAW_AIP_DIR / "split"  # フィーチャー種別分割ファイル

PROCESSED_DIR = DATA_DIR / "processed"
DB_DIR = PROCESSED_DIR / "duckdb"
GEOPARQUET_DIR = PROCESSED_DIR / "geoparquet"

# ログディレクトリ
LOG_DIR = BASE_DIR / "logs"

# 出力ファイルパス
NOTAM_DB_PATH = DB_DIR / "notam.duckdb"
AIP_DB_PATH = DB_DIR / "aip.duckdb"
AIP_ARCHIVE_DB_PATH = DB_DIR / "aip_archive.duckdb"  # 全TimeSliceを保持するアーカイブ

NOTAM_PARQUET_PATH = GEOPARQUET_DIR / "notams.parquet"
AIP_PARQUET_DIR = GEOPARQUET_DIR / "aip"  # フィーチャー種別ごとにparquetを出力

# Webサーバー設定
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000

# デバッグモード設定
# Trueの場合、WebサーバーはアーカイブDBに直接接続（全TimeSliceを参照）
# Falseの場合、実運用DBに接続（通常運用）
DEBUG_USE_ARCHIVE_DB = os.getenv("DEBUG_USE_ARCHIVE_DB", "false").lower() == "true"

# 地図タイル設定 (CartoDB Positron - 無料・軽量・薄色)
MAP_TILE_URL = "https://cartodb-basemaps-{s}.global.ssl.fastly.net/light_all/{z}/{x}/{y}.png"
MAP_TILE_ATTRIBUTION = "© OpenStreetMap contributors © CARTO"
MAP_DEFAULT_CENTER = (35.5, 139.5)  # 日本中央付近
MAP_DEFAULT_ZOOM = 6


@dataclass
class AMQPConfig:
    """SWIM AMQP接続設定"""
    broker_url: str = ""  # amqp://host:port 形式
    auth_id: str = ""  # キュー接続用認証ID
    auth_password: str = ""  # キュー接続用パスワード
    receive_queue_id: str = ""  # 受信キューID
    send_queue_id: str = ""  # 送信キューID

    # NOTAM配信サービス (P2004) - Pub/Sub
    notam_topic: str = ""  # インターネット経由利用者向け
    notam_msg_def: str = ""

    # AIPデータ配信サービス (M2001) - メッセージ転送
    aip_msg_def: str = ""

    # 無通信タイムアウト (秒) - 1時間
    idle_timeout: int = 3600

    # ハートビート間隔 (秒)
    heartbeat_interval: int = 300


@dataclass
class SWIMWebAPIConfig:
    """SWIM WebAPI接続設定 (デジタルノータムリクエストサービス等)"""
    login_url: str = "https://top.swim.mlit.go.jp/xxxxx/webapi/login"
    search_url: str = "https://web.swim.mlit.go.jp/xxxxx/web/search"
    user_id: str = ""
    password: str = ""
    timeout: int = 30  # 秒
    # モックモード (SWIMアカウント未設定時やオフライン時に自動でサンプルデータを返す)
    use_mock_fallback: bool = True


# WebAPI設定インスタンス (環境変数から読み込み可能)
swim_web_api_config = SWIMWebAPIConfig(
    login_url=os.getenv("SWIM_LOGIN_URL", "https://top.swim.mlit.go.jp/xxxxx/webapi/login"),
    search_url=os.getenv("SWIM_SEARCH_URL", "https://web.swim.mlit.go.jp/xxxxx/web/search"),
    user_id=os.getenv("SWIM_USER_ID", ""),
    password=os.getenv("SWIM_PASSWORD", ""),
    timeout=int(os.getenv("SWIM_TIMEOUT", "30")),
    use_mock_fallback=os.getenv("SWIM_USE_MOCK_FALLBACK", "true").lower() == "true",
)


# AMQP設定インスタンス (環境変数から読み込み可能)
amqp_config = AMQPConfig(
    broker_url=os.getenv("SWIM_AMQP_BROKER_URL", ""),
    auth_id=os.getenv("SWIM_AMQP_AUTH_ID", ""),
    auth_password=os.getenv("SWIM_AMQP_AUTH_PASSWORD", ""),
    receive_queue_id=os.getenv("SWIM_AMQP_RECEIVE_QUEUE_ID", ""),
    send_queue_id=os.getenv("SWIM_AMQP_SEND_QUEUE_ID", ""),
)


# AIXM名前空間定義
AIXM_NAMESPACES = {
    "message": "http://www.aixm.aero/schema/5.1.1/message",
    "aixm": "http://www.aixm.aero/schema/5.1.1",
    "gml": "http://www.opengis.net/gml/3.2",
    "event": "http://www.aixm.aero/schema/5.1/event",
    "xlink": "http://www.w3.org/1999/xlink",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "gco": "http://www.isotc211.org/2005/gco",
    "gss": "http://www.isotc211.org/2005/gss",
    "gsr": "http://www.isotc211.org/2005/gsr",
    "gts": "http://www.isotc211.org/2005/gts",
    "gmd": "http://www.isotc211.org/2005/gmd",
    "xsd": "http://www.w3.org/2001/XMLSchema",
}


# フィーチャー種別（AIP分割用）
AIP_FEATURE_TYPES = {
    "AirportHeliport": "空港・ヘリポート",
    "Runway": "滑走路",
    "RunwayDirection": "滑走路方向",
    "RunwayElement": "滑走路要素",
    "Taxiway": "誘導路",
    "TaxiwayElement": "誘導路要素",
    "Apron": "エプロン",
    "ApronElement": "エプロン要素",
    "AircraftStand": "スポット",
    "Airspace": "空域",
    "Navaid": "無線施設",
    "Obstacle": "障害物件",
    "DesignatedPoint": "指定点",
    "Route": "経路",
    "Other": "その他",
}


# NOTAMシナリオ種別
NOTAM_SCENARIOS = {
    "AD.CLS/LIM.JP": "飛行場/ヘリポートの閉鎖・制限",
    "RWY.CLS/LIM.JP": "滑走路の閉鎖・制限",
    "TWY.CLS/LIM.JP": "誘導路の閉鎖・制限",
    "APN.CLS/LIM.JP": "エプロンの閉鎖・制限",
    "STAND.CLS/LIM.JP": "スポットの閉鎖・制限",
    "SFC.CON.RWY.JP": "スノータム",
    "SFC.CON.TWY.JP": "誘導路/エプロンのブレーキングアクション",
    "RCLL.UNS.JP": "滑走路中心線灯の機能停止",
    "TWCL.UNS.JP": "誘導路中心線灯の機能停止",
    "OBL.UNS.JP": "航空障害灯の機能停止",
    "OBM.UNS.JP": "昼間障害標識の機能停止",
    "ILS.UNS.JP": "無線施設(ILS)の運用停止",
    "NAV.UNS.JP": "無線施設(ILS以外)の運用停止",
    "RAIM.OUT.JP": "RAIM予測情報等の提供",
    "OBS.NEW.JP": "障害物件",
    "OBS.PSS.JP": "障害物件(船舶の通過)",
    "SAA.ACT.JP": "公示空域の使用",
    "SAA.NEW.JP": "航空機の航行に影響を及ぼす行為等",
    "FLW.CTL.JP": "交通流制御",
    "CDR.ACT.JP": "CDR2の運用",
    "PACOTS.JP": "PACOTSの設定",
    "OTH.EVT.JP": "その他の事象",
    "OTH.SPC.JP": "その他の事象（特殊）",
    "F.NTM.JP": "外国ノータム",
    "J.NTM.JP": "自衛隊ノータム",
    "SFC.CON.F.JP": "外国SNOWTAM",
}


# 高度フィルター設定
ALTITUDE_MAX_FT = 14000  # FL140 (転移高度)
ALTITUDE_MAX_M = 4200    # メートル換算
ALTITUDE_PRESETS = {
    "drone": {"label": "ドローン", "min_ft": 0, "max_ft": 500, "min_m": 0, "max_m": 150},
    "vfr": {"label": "低空 (VFR)", "min_ft": 0, "max_ft": 3000, "min_m": 0, "max_m": 900},
    "max": {"label": "全域", "min_ft": 0, "max_ft": 14000, "min_m": 0, "max_m": 4200},
}