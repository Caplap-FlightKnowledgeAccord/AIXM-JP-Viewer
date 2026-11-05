"""ICAO NOTAM 電文フォーマッター

AIXM 5.1 / Digital NOTAM の XML メッセージから要素を抽出し、
標準的な ICAO NOTAM 電文フォーマット (Q行, A〜G項) を生成・整形する。
"""

import re
import logging
from typing import Dict, Any, List, Optional
from lxml import etree

logger = logging.getLogger(__name__)


class ICAONOTAMFormatter:
    """AIXM 5.1 XML から ICAO 標準 NOTAM 電文を生成するフォーマッター"""

    @classmethod
    def format_xml(cls, xml_content: str) -> Dict[str, Any]:
        """AIXM XML文字列をパースし、ICAO電文テキストを含む辞書を返す"""
        if not xml_content or not xml_content.strip():
            return {}

        try:
            root = etree.fromstring(xml_content.encode("utf-8") if isinstance(xml_content, str) else xml_content)
        except Exception as e:
            logger.warning(f"XML parse error in ICAONOTAMFormatter: {e}")
            return {"raw_text": xml_content}

        return cls.format_element(root)

    @classmethod
    def format_element(cls, root: etree.Element) -> Dict[str, Any]:
        """lxml ElementからNOTAM情報を抽出してICAO電文テキストを作成"""
        # 1. 直接 textNOTAM / icaoNOTAM 等の電文が入っているかチェック
        raw_icao_text = cls._get_text_by_localname(root, "textnotam") or \
                         cls._get_text_by_localname(root, "icaonotam") or \
                         cls._get_text_by_localname(root, "formattedtext")

        # 2. 各構成要素の抽出
        series = cls._get_text_by_localname(root, "series") or ""
        number = cls._get_text_by_localname(root, "number") or cls._get_text_by_localname(root, "notamnr") or ""
        year = cls._get_text_by_localname(root, "year") or ""
        notam_type_raw = cls._get_text_by_localname(root, "notamtype") or cls._get_text_by_localname(root, "type") or "N"
        notam_type = f"NOTAM{notam_type_raw}" if not notam_type_raw.startswith("NOTAM") else notam_type_raw
        ref_notam = cls._get_text_by_localname(root, "referencednotam") or cls._get_text_by_localname(root, "refnotam")

        fir = cls._get_text_by_localname(root, "affectedfir") or cls._get_text_by_localname(root, "fir") or "RJJJ"
        q_code = cls._get_text_by_localname(root, "selectioncode") or cls._get_text_by_localname(root, "qcode") or "QXXXX"
        traffic = cls._get_text_by_localname(root, "traffic") or "IV"
        purpose = cls._get_text_by_localname(root, "purpose") or "NBO"
        scope = cls._get_text_by_localname(root, "scope") or "A"
        min_fl = cls._get_text_by_localname(root, "minimumfl") or cls._get_text_by_localname(root, "lowerfl") or "000"
        max_fl = cls._get_text_by_localname(root, "maximumfl") or cls._get_text_by_localname(root, "upperfl") or "999"
        
        coords = cls._get_text_by_localname(root, "coordinates") or ""
        radius = cls._get_text_by_localname(root, "radius") or ""
        coords_radius = f"{coords}{radius}" if (coords and radius) else (coords or cls._get_text_by_localname(root, "coordinatesradius") or "")

        location = cls._get_text_by_localname(root, "location") or cls._get_text_by_localname(root, "designator") or ""
        effective_start = cls._get_text_by_localname(root, "effectivestart") or cls._get_text_by_localname(root, "beginposition") or ""
        effective_end = cls._get_text_by_localname(root, "effectiveend") or cls._get_text_by_localname(root, "endposition") or ""
        schedule = cls._get_text_by_localname(root, "schedule") or cls._get_text_by_localname(root, "timesheet") or ""
        body_text = cls._get_text_by_localname(root, "text") or cls._get_text_by_localname(root, "eitem") or ""
        lower_limit = cls._get_text_by_localname(root, "lowerlimit") or ""
        upper_limit = cls._get_text_by_localname(root, "upperlimit") or ""

        # NOTAM番号フォーマット (例: A0123/26)
        notam_id = ""
        if series and number:
            padded_num = number.zfill(4) if len(number) < 4 else number
            if year:
                yy = year[-2:] if len(year) >= 2 else year
                notam_id = f"{series}{padded_num}/{yy}"
            else:
                notam_id = f"{series}{padded_num}"
        elif number:
            notam_id = number

        # 日時フォーマット (YYMMDDhhmm 形式)
        start_str = cls._format_dt_yyyymmddhhmm(effective_start)
        end_str = cls._format_dt_yyyymmddhhmm(effective_end)
        if not end_str:
            end_str = "PERM"

        # 既存の textNOTAM がある場合、それが完全なICAO電文ならそれを活かす
        if raw_icao_text and ("Q)" in raw_icao_text or "A)" in raw_icao_text):
            icao_text = raw_icao_text.strip()
        else:
            # ICAO NOTAM 電文を合成組み立て
            icao_text = cls._build_icao_text(
                notam_id=notam_id or "A0000/00",
                notam_type=notam_type,
                ref_notam=ref_notam,
                fir=fir,
                q_code=q_code,
                traffic=traffic,
                purpose=purpose,
                scope=scope,
                min_fl=str(min_fl).zfill(3),
                max_fl=str(max_fl).zfill(3),
                coords_radius=coords_radius,
                location=location,
                start_str=start_str,
                end_str=end_str,
                schedule=schedule,
                body_text=body_text or raw_icao_text or "(NO TEXT)",
                lower_limit=lower_limit,
                upper_limit=upper_limit,
            )

        return {
            "notam_id": notam_id,
            "location": location,
            "fir": fir,
            "q_code": q_code,
            "effective_start": effective_start,
            "effective_end": effective_end,
            "icao_text": icao_text,
            "body_text": body_text or raw_icao_text or "",
            "schedule": schedule,
        }

    @classmethod
    def _build_icao_text(
        cls,
        notam_id: str,
        notam_type: str,
        ref_notam: Optional[str],
        fir: str,
        q_code: str,
        traffic: str,
        purpose: str,
        scope: str,
        min_fl: str,
        max_fl: str,
        coords_radius: str,
        location: str,
        start_str: str,
        end_str: str,
        schedule: str,
        body_text: str,
        lower_limit: str,
        upper_limit: str,
    ) -> str:
        """ICAO標準電文テキストを組み立てる"""
        lines = []

        # ヘッダー行: (A0123/26 NOTAMN [A0100/26]
        header_parts = [notam_id, notam_type]
        if ref_notam:
            header_parts.append(ref_notam)
        lines.append(f"({' '.join(header_parts)}")

        # Q行: Q) RJJJ/QMRLC/IV/NBO/A/000/999/3533N13947E005
        q_clean = q_code.strip()
        if not q_clean.startswith("Q"):
            q_clean = f"Q{q_clean}"
        q_parts = [fir or "RJJJ", q_clean, traffic or "IV", purpose or "NBO", scope or "A", min_fl or "000", max_fl or "999"]
        if coords_radius:
            q_parts.append(coords_radius)
        lines.append(f"Q) {'/'.join(q_parts)}")

        # A) B) C) 行
        abc_line = f"A) {location or '????'} B) {start_str or '0000000000'} C) {end_str or 'PERM'}"
        lines.append(abc_line)

        # D) 行 (スケジュールがあれば)
        if schedule:
            lines.append(f"D) {schedule}")

        # E) 本文
        lines.append(f"E) {body_text}")

        # F) G) 行 (高度制限があれば)
        if lower_limit or upper_limit:
            fg_parts = []
            if lower_limit:
                fg_parts.append(f"F) {lower_limit}")
            if upper_limit:
                fg_parts.append(f"G) {upper_limit}")
            lines.append(" ".join(fg_parts) + ")")
        else:
            # 末尾に閉じ括弧をつける
            lines[-1] = lines[-1] + ")"

        return "\n".join(lines)

    @staticmethod
    def _format_dt_yyyymmddhhmm(dt_str: str) -> str:
        """ISO文字列や日時文字列を YYMMDDhhmm 形式に変換"""
        if not dt_str:
            return ""
        # 既に YYMMDDhhmm 形式に近い場合
        digits = re.sub(r"\D", "", dt_str)
        if len(digits) == 12:  # YYYYMMDDhhmm
            return digits[2:]
        if len(digits) == 10:  # YYMMDDhhmm
            return digits
        if len(digits) == 14:  # YYYYMMDDhhmmss
            return digits[2:12]
        return dt_str.replace("-", "").replace(":", "").replace("T", "").replace("Z", "")[:10]

    @staticmethod
    def _get_text_by_localname(root: etree.Element, local_name: str) -> Optional[str]:
        """local-name (大文字小文字無視) で要素テキストを探索"""
        target = local_name.lower()
        for elem in root.iter():
            qname = etree.QName(elem).localname.lower()
            if qname == target:
                if elem.text and elem.text.strip():
                    return elem.text.strip()
        return None
