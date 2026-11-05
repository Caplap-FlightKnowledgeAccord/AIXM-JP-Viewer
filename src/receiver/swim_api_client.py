"""SWIM WebAPI クライアント

国土交通省 SWIMサービス WebAPI (ログイン・デジタルノータム検索要求) との連携を処理するモジュール。
"""

import os
import re
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import requests

from config.settings import swim_web_api_config
from src.parsers.icao_notam_formatter import ICAONOTAMFormatter

logger = logging.getLogger(__name__)


class SWIMAPIClient:
    """SWIM WebAPI 連携クライアント"""

    def __init__(self, config=None):
        self.config = config or swim_web_api_config
        self.session = requests.Session()
        self.is_logged_in = False

    def login(self) -> bool:
        """SWIM ログインAPI を実行し、認証Cookieをセッションに保持する"""
        if not self.config.user_id or not self.config.password:
            logger.warning("SWIM API アカウント情報 (ID/Password) が設定されていません。")
            return False

        payload = {
            "id": self.config.user_id,
            "password": self.config.password,
        }
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
        }

        try:
            logger.info(f"SWIM ログイン要求送信: {self.config.login_url}")
            resp = self.session.post(
                self.config.login_url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                err_code = data.get("error_info", {}).get("error_code")
                if err_code == 0 or err_code == "0":
                    logger.info("✅ SWIM ログイン成功")
                    # Cookieの適用範囲をルートパスおよび全SWIMドメインに拡張
                    cookies_dict = self.session.cookies.get_dict()
                    for name, value in cookies_dict.items():
                        self.session.cookies.set(name, value, domain=".swim.mlit.go.jp", path="/")
                        self.session.cookies.set(name, value, domain="web.swim.mlit.go.jp", path="/")
                        self.session.cookies.set(name, value, domain=".mlit.go.jp", path="/")
                    self.is_logged_in = True
                    return True
                else:
                    logger.error(f"❌ SWIM ログインエラー: {data.get('error_info')}")
                    return False
            else:
                logger.error(f"❌ SWIM ログイン失敗 HTTPステータス: {resp.status_code}, 内容: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"❌ SWIM ログイン例外: {e}")
            return False

    def search_digital_notam(
        self,
        locations: Optional[str] = None,
        valid_start: Optional[str] = None,
        valid_end: Optional[str] = None,
        keyword: Optional[str] = None,
        series: Optional[str] = None,
        notam_code: Optional[str] = None,
        fir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """デジタルノータム検索APIを実行し、パース済みの電文リストを返す"""
        # アカウント情報が未設定またはモック使用が有効な場合で接続できないときのフォールバック
        if not self.config.user_id:
            if self.config.use_mock_fallback:
                logger.info("SWIM アカウント未設定のため、モックNOTAMデータを生成して返却します。")
                return self._generate_mock_notams(locations or "RJTT+RJAA")
            return []

        if not self.is_logged_in:
            if not self.login():
                if self.config.use_mock_fallback:
                    logger.warning("SWIM ログイン失敗。モックNOTAMデータを返却します。")
                    return self._generate_mock_notams(locations or "RJTT+RJAA")
                return []

        # クエリパラメータ構築
        params: Dict[str, Any] = {
            "userId": self.config.user_id,
            "display": "0",  # 有効ノータム表示
        }
        if locations:
            # 複数地点は "+" で結合
            loc_list = [loc.strip().upper() for loc in re.split(r"[,+\s]+", locations) if loc.strip()]
            params["location"] = "+".join(loc_list)
        else:
            params["location"] = "RJTT"

        # 開始・終了日時のフォーマット (validDatetimeEnd はSWIM仕様上必須)
        now_utc = datetime.now(timezone.utc)
        if valid_start:
            params["validDatetimeStart"] = self._format_param_dt(valid_start)
        else:
            params["validDatetimeStart"] = now_utc.strftime("%Y%m%d%H%M")

        if valid_end:
            params["validDatetimeEnd"] = self._format_param_dt(valid_end)
        else:
            # 未指定時は30日後を自動設定
            from datetime import timedelta
            end_dt = now_utc + timedelta(days=30)
            params["validDatetimeEnd"] = end_dt.strftime("%Y%m%d%H%M")

        if keyword:
            params["keyword"] = keyword
            params["andOrCondition"] = "0"  # 0: AND, 1: OR
        if series:
            params["series"] = series.upper()
        if notam_code:
            params["notamCode"] = notam_code.upper()
        if fir:
            params["fir"] = fir.upper()

        try:
            logger.info(f"SWIM デジタルノータム検索要求: {self.config.search_url}, パラメータ: {params}")
            resp = self.session.get(
                self.config.search_url,
                params=params,
                timeout=self.config.timeout,
            )

            # セッション失効時は1度だけ再ログインしてリトライ
            if resp.status_code in (401, 403) or (resp.status_code == 200 and "Authentication Request" in resp.text):
                logger.warning("セッション失効を検知。再ログインして再試行します。")
                if self.login():
                    resp = self.session.get(
                        self.config.search_url,
                        params=params,
                        timeout=self.config.timeout,
                    )

            if resp.status_code == 200:
                result_json = resp.json()
                return self._parse_search_response(result_json)
            else:
                logger.error(f"SWIM 検索APIエラー: HTTP {resp.status_code} - {resp.text}")
                if self.config.use_mock_fallback:
                    return self._generate_mock_notams(locations or "RJTT+RJAA")
                return []
        except Exception as e:
            logger.error(f"SWIM 検索API通信例外: {e}")
            if self.config.use_mock_fallback:
                logger.info("通信例外のためモックNOTAMデータを返却します。")
                return self._generate_mock_notams(locations or "RJTT+RJAA")
            return []

    def _parse_search_response(self, result_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        """SWIM検索レスポンスJSONからXMLを取り出し、ICAONOTAMFormatterで電文リストに変換"""
        notam_list = []
        raw_data = result_json.get("data")
        if isinstance(raw_data, dict):
            digital_notams = raw_data.get("digitalNotam", [])
            for xml_str in digital_notams:
                if not xml_str:
                    continue
                parsed = ICAONOTAMFormatter.format_xml(xml_str)
                if parsed:
                    notam_list.append(parsed)
        elif isinstance(raw_data, list):
            for data_item in raw_data:
                digital_notams = data_item.get("digitalNotam", [])
                for xml_str in digital_notams:
                    if not xml_str:
                        continue
                    parsed = ICAONOTAMFormatter.format_xml(xml_str)
                    if parsed:
                        notam_list.append(parsed)
        return notam_list

    @staticmethod
    def _format_param_dt(dt_str: str) -> str:
        """YYYYMMDDhhmm 形式に変換"""
        digits = re.sub(r"\D", "", dt_str)
        if len(digits) >= 12:
            return digits[:12]
        return dt_str

    def _generate_mock_notams(self, locations_str: str) -> List[Dict[str, Any]]:
        """開発・テスト用のリアルなモックNOTAM電文リストを生成"""
        locs = [loc.strip().upper() for loc in re.split(r"[,+\s]+", locations_str) if loc.strip()]
        if not locs:
            locs = ["RJTT", "RJAA"]

        now = datetime.now(timezone.utc)
        curr_year = now.strftime("%y")

        mock_templates = [
            {
                "location": "RJTT",
                "notam_id": f"A0142/{curr_year}",
                "fir": "RJJJ",
                "q_code": "QMRLC",
                "traffic": "IV",
                "purpose": "NBO",
                "scope": "A",
                "coords": "3533N13947E005",
                "start": f"{curr_year}08150000",
                "end": f"{curr_year}08252359",
                "schedule": "DAILY 1500-2100",
                "text": "RWY 16L/34R CLSD DUE TO WIP.",
                "lower": "",
                "upper": "",
            },
            {
                "location": "RJTT",
                "notam_id": f"A0145/{curr_year}",
                "fir": "RJJJ",
                "q_code": "QOBCE",
                "traffic": "IV",
                "purpose": "M",
                "scope": "AE",
                "coords": "3533N13946E001",
                "start": f"{curr_year}08100100",
                "end": "PERM",
                "schedule": "",
                "text": "OBST CRANE ERECTED AT PSN 353308N 1394625E (APRX 1200M NW OF ARP). ELEV 245FT AMSL. LGTD.",
                "lower": "SFC",
                "upper": "245FT AMSL",
            },
            {
                "location": "RJAA",
                "notam_id": f"A0088/{curr_year}",
                "fir": "RJJJ",
                "q_code": "QMXLC",
                "traffic": "IV",
                "purpose": "M",
                "scope": "A",
                "coords": "3545N14023E005",
                "start": f"{curr_year}08120000",
                "end": f"{curr_year}08311200",
                "schedule": "",
                "text": "TWY B1, B2 CLSD FOR ALL ACFT DUE TO MAINT.",
                "lower": "",
                "upper": "",
            },
            {
                "location": "RJAA",
                "notam_id": f"A0091/{curr_year}",
                "fir": "RJJJ",
                "q_code": "QICAS",
                "traffic": "I",
                "purpose": "NBO",
                "scope": "A",
                "coords": "3546N14023E005",
                "start": f"{curr_year}08140300",
                "end": f"{curr_year}08201000",
                "schedule": "",
                "text": "ILS RWY 16R GP U/S DUE TO PERIODICAL CHECK.",
                "lower": "",
                "upper": "",
            },
            {
                "location": "RJBB",
                "notam_id": f"A0054/{curr_year}",
                "fir": "RJJJ",
                "q_code": "QNVAS",
                "traffic": "IV",
                "purpose": "BO",
                "scope": "AE",
                "coords": "3426N13514E025",
                "start": f"{curr_year}08010000",
                "end": f"{curr_year}08312359",
                "schedule": "",
                "text": "KIX VOR/DME 114.1MHZ CH88X U/S DUE TO MAINT.",
                "lower": "SFC",
                "upper": "UNL",
            },
            {
                "location": "RJCC",
                "notam_id": f"A0033/{curr_year}",
                "fir": "RJJJ",
                "q_code": "QMRLC",
                "traffic": "IV",
                "purpose": "NBO",
                "scope": "A",
                "coords": "4246N14140E005",
                "start": f"{curr_year}08151400",
                "end": f"{curr_year}08152100",
                "schedule": "",
                "text": "RWY 01R/19L CLSD DUE TO RUBBER REMOVAL OPERATION.",
                "lower": "",
                "upper": "",
            }
        ]

        results = []
        for t in mock_templates:
            if t["location"] in locs or "ALL" in locs:
                icao_text = ICAONOTAMFormatter._build_icao_text(
                    notam_id=t["notam_id"],
                    notam_type="NOTAMN",
                    ref_notam=None,
                    fir=t["fir"],
                    q_code=t["q_code"],
                    traffic=t["traffic"],
                    purpose=t["purpose"],
                    scope=t["scope"],
                    min_fl="000",
                    max_fl="999",
                    coords_radius=t["coords"],
                    location=t["location"],
                    start_str=t["start"],
                    end_str=t["end"],
                    schedule=t["schedule"],
                    body_text=t["text"],
                    lower_limit=t["lower"],
                    upper_limit=t["upper"],
                )
                results.append({
                    "notam_id": t["notam_id"],
                    "location": t["location"],
                    "fir": t["fir"],
                    "q_code": t["q_code"],
                    "effective_start": t["start"],
                    "effective_end": t["end"],
                    "icao_text": icao_text,
                    "body_text": t["text"],
                    "schedule": t["schedule"],
                })

        # もし対象空港が見つからない場合でも、リクエストされた空港のダミーを作成
        if not results and locs:
            for loc in locs:
                dummy_id = f"A0001/{curr_year}"
                icao_text = ICAONOTAMFormatter._build_icao_text(
                    notam_id=dummy_id,
                    notam_type="NOTAMN",
                    ref_notam=None,
                    fir="RJJJ",
                    q_code="QFAXX",
                    traffic="IV",
                    purpose="NBO",
                    scope="A",
                    min_fl="000",
                    max_fl="999",
                    coords_radius="",
                    location=loc,
                    start_str=f"{curr_year}08150000",
                    end_str="PERM",
                    schedule="",
                    body_text=f"NO ACTIVE RESTRICTIONS REPORTED FOR {loc}.",
                    lower_limit="",
                    upper_limit="",
                )
                results.append({
                    "notam_id": dummy_id,
                    "location": loc,
                    "fir": "RJJJ",
                    "q_code": "QFAXX",
                    "effective_start": f"{curr_year}08150000",
                    "effective_end": "PERM",
                    "icao_text": icao_text,
                    "body_text": f"NO ACTIVE RESTRICTIONS REPORTED FOR {loc}.",
                    "schedule": "",
                })

        return results
