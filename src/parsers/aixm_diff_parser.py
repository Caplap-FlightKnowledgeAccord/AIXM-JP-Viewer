"""
AIXM差分XMLパーサー（仕様書 3.2, 3.3, 4.1 完全対応版）

AIXM 5.1 に物理削除（DELETE）の概念は存在しない。
すべての状態変更・廃止は validTime および featureLifetime のプロパティ更新によって表現される。

- INSERT: 新しい gml_id をアーカイブDBに追加 (BASELINEタイムスライス)
- UPDATE: 対象 gml_id の属性、幾何情報、または有効開始日時 (valid_from) を更新 (BASELINEタイムスライス)
- WITHDRAWAL: 対象 gml_id の有効終了日時 (valid_to) に廃止発効日時を設定（物理削除なし）(PERMDELTA / DELETEタイムスライス)

DIFFファイル内の各 Feature には複数のタイムスライスが含まれるため、
タイムスライス単位で変更タイプを判定する。
"""

import os
import json
from lxml import etree
from typing import Dict, List, Any

from src.parsers.aixm_aip_parser import AIXMAipParser, NS_AIXM, NS_GML, NS_XLINK, NS_MESSAGE

# 対象Feature種別
DIFF_FEATURE_TYPES = [
    'AirportHeliport', 'Runway', 'RunwayDirection', 'RunwayElement',
    'Taxiway', 'TaxiwayElement', 'Apron', 'ApronElement', 'AircraftStand',
    'Airspace', 'Navaid', 'Obstacle', 'DesignatedPoint', 'Route', 'RoutePiece',
    'HoldingPattern', 'StandardInstrumentArrival', 'StandardInstrumentDeparture'
]


class AIXMDiffParser:
    """AIXM差分XMLパーサー（仕様書 3.2, 3.3 準拠）"""

    @staticmethod
    def parse_diff_file(xml_path: str) -> Dict[str, List[Any]]:
        """
        差分XMLファイルをパースして変更タイプ別に分類する。

        AIXM DIFFでは各 Feature に複数のタイムスライスが含まれる。
        タイムスライス単位で変更タイプを判定する。

        Returns:
            {
                'insert': [...],      # 新規追加 Feature (BASELINEタイムスライス)
                'update': [...],      # 属性・幾何変更 Feature (BASELINEタイムスライス)
                'withdrawal': [...],  # 施設廃止 Feature (PERMDELTA / DELETEタイムスライス)
                'metadata': {...}
            }
        """
        result = {
            'insert': [],
            'update': [],
            'withdrawal': [],
            'metadata': {}
        }

        if not os.path.exists(xml_path):
            print(f"ファイルが存在しません: {xml_path}")
            return result

        try:
            # lxml でストリーミングパース
            context = etree.iterparse(
                xml_path,
                events=('end',),
                tag=f'{{{NS_MESSAGE}}}hasMember',
                huge_tree=True,
                recover=True,
            )

            for event, member_elem in context:
                AIXMDiffParser._parse_member(member_elem, result)

                # メモリ解放
                member_elem.clear()
                while member_elem.getprevious() is not None:
                    del member_elem.getparent()[0]

            print(f"  変更タイプ別件数: INSERT={len(result['insert'])}, "
                  f"UPDATE={len(result['update'])}, WITHDRAWAL={len(result['withdrawal'])}")

        except Exception as e:
            print(f"差分XML解析エラー ({xml_path}): {e}")

        return result

    @staticmethod
    def _parse_member(member_elem: etree._Element, result: Dict[str, List[Any]]):
        """
        1つの hasMember 要素を解析し、タイムスライス単位で変更タイプを判定する。

        AIXM DIFFのFeatureは以下のタイムスライスを含む:
        - BASELINE: 現在有効な状態 (INSERT/UPDATE)
        - PERMDELTA: 永久廃止 (WITHDRAWAL)
        - DELETE解釈: 廃止 (WITHDRAWAL)
        """
        # Feature要素を抽出
        feature_type, feature_elem = AIXMDiffParser._extract_feature(member_elem)
        if not feature_type or feature_elem is None:
            return

        # gml_id 抽出
        gml_id = ""
        for attr_name, attr_val in feature_elem.attrib.items():
            if 'id' in attr_name:
                gml_id = AIXMAipParser._clean_uuid(attr_val)
                break

        # タイムスライスごとの解釈を確認
        timeslices = feature_elem.findall(f'.//{{{NS_AIXM}}}timeSlice')
        if not timeslices:
            return

        # タイムスライスを解釈タイプ別に分類
        baseline_slices = []
        permdelta_slices = []

        for ts_elem in timeslices:
            # interpretation はサブタイムスライス要素内にあるため .// で検索
            interp = ts_elem.find(f'.//{{{NS_AIXM}}}interpretation')
            interp_value = interp.text.strip().upper() if interp is not None and interp.text else ''

            if interp_value in ('PERMDELTA', 'DELETE'):
                permdelta_slices.append(ts_elem)
            elif interp_value in ('BASELINE', 'TEMPDELTA'):
                baseline_slices.append(ts_elem)

        # WITHDRAWAL: PERMDELTAタイムスライスから廃止発効日時を取得
        # 複数のPERMDELTAタイムスライスがある場合は最新の日時を採用する
        withdrawal_time = None
        for ts_elem in permdelta_slices:
            ts_time = AIXMDiffParser._extract_valid_to(ts_elem)
            if ts_time is not None:
                if withdrawal_time is None or ts_time > withdrawal_time:
                    withdrawal_time = ts_time

        if withdrawal_time is None:
            withdrawal_time = AIXMDiffParser._extract_feature_lifetime_end(feature_elem)

        # 廃止対象が存在する場合のみ WITHDRAWAL として登録
        if permdelta_slices:
            result['withdrawal'].append({
                'gml_id': gml_id,
                'valid_to': withdrawal_time,
            })

        # INSERT/UPDATE: BASELINEタイムスライスからレコードを生成
        for ts_elem in baseline_slices:
            record = AIXMAipParser.extract_feature_record_from_timeslice(
                feature_elem, ts_elem, feature_type
            )
            if record is None:
                continue

            # 既存か新規かを判定
            # (厳密な判定は適用時に実施するため、ここでは全て update に分類)
            result['update'].append(record)

    @staticmethod
    def _extract_feature(member_elem: etree._Element):
        """Feature要素とその種別を抽出"""
        for child in member_elem:
            tag = child.tag
            local_name = tag.split('}', 1)[1] if '}' in tag else tag
            if local_name in DIFF_FEATURE_TYPES:
                return local_name, child
        return None, None

    @staticmethod
    def _extract_valid_to(ts_elem: etree._Element):
        """
        タイムスライスの有効終了日時を抽出。

        PERMDELTAタイムスライスでは validTime が TimeInstant で表され、
        timePosition が廃止発効日時となる。
        """
        valid_time = ts_elem.find(f'.//{{{NS_GML}}}validTime')
        if valid_time is not None:
            # TimePeriod の場合
            period = valid_time.find(f'{{{NS_GML}}}TimePeriod')
            if period is not None:
                end = period.find(f'{{{NS_GML}}}endPosition')
                if end is not None and end.text:
                    return AIXMAipParser._parse_datetime(end.text)

            # TimeInstant の場合 (PERMDELTA)
            time_instant = valid_time.find(f'{{{NS_GML}}}TimeInstant')
            if time_instant is not None:
                time_pos = time_instant.find(f'{{{NS_GML}}}timePosition')
                if time_pos is not None and time_pos.text:
                    return AIXMAipParser._parse_datetime(time_pos.text)

        return None

    @staticmethod
    def _extract_feature_lifetime_end(feature_elem: etree._Element):
        """Featureのライフタイム終了日時を抽出"""
        lifetime = feature_elem.find(f'.//{{{NS_AIXM}}}featureLifetime')
        if lifetime is not None:
            period = lifetime.find(f'{{{NS_GML}}}TimePeriod')
            if period is not None:
                end = period.find(f'{{{NS_GML}}}endPosition')
                if end is not None and end.text:
                    return AIXMAipParser._parse_datetime(end.text)

        return None

    @staticmethod
    def _determine_change_type(member_elem: etree._Element) -> str:
        """
        変更タイプを判定する (仕様書 3.3)。
        互換用: タイムスライス全体の解釈から大まかな種別を判定する。
        """
        interpretations = member_elem.findall(f'.//{{{NS_AIXM}}}interpretation')
        interp_values = [
            interp.text.strip().upper()
            for interp in interpretations
            if interp is not None and interp.text and interp.text.strip()
        ]

        if 'DELETE' in interp_values:
            return 'withdrawal'

        if 'PERMDELTA' in interp_values and 'BASELINE' not in interp_values:
            return 'withdrawal'

        if 'BASELINE' in interp_values:
            return 'insert' if len(interp_values) <= 1 else 'update'

        return 'update'

    @staticmethod
    def _extract_gml_id(member_elem: etree._Element) -> str:
        """Featureのgml_id (UUID) を抽出"""
        for attr_name, attr_val in member_elem.attrib.items():
            if 'id' in attr_name:
                return AIXMAipParser._clean_uuid(attr_val)

        identifier = member_elem.find(f'.//{{{NS_GML}}}identifier')
        if identifier is not None and identifier.text:
            return AIXMAipParser._clean_uuid(identifier.text)

        for child in member_elem:
            tag = child.tag
            local_name = tag.split('}', 1)[1] if '}' in tag else tag
            for attr_name, attr_val in child.attrib.items():
                if 'id' in attr_name:
                    return AIXMAipParser._clean_uuid(attr_val)

        return ''

    @staticmethod
    def _extract_withdrawal_time(member_elem: etree._Element):
        """廃止 (WITHDRAWAL) 時の有効終了日時を抽出する"""
        valid_time = member_elem.find(f'.//{{{NS_GML}}}validTime')
        if valid_time is not None:
            period = valid_time.find(f'{{{NS_GML}}}TimePeriod')
            if period is not None:
                end = period.find(f'{{{NS_GML}}}endPosition')
                if end is not None and end.text:
                    return AIXMAipParser._parse_datetime(end.text)

        lifetime = member_elem.find(f'.//{{{NS_AIXM}}}featureLifetime')
        if lifetime is not None:
            period = lifetime.find(f'{{{NS_GML}}}TimePeriod')
            if period is not None:
                end = period.find(f'{{{NS_GML}}}endPosition')
                if end is not None and end.text:
                    return AIXMAipParser._parse_datetime(end.text)

        return None

    @staticmethod
    def _extract_feature_type(member_elem: etree._Element) -> str:
        """Feature種別を抽出"""
        for child in member_elem:
            tag = child.tag
            local_name = tag.split('}', 1)[1] if '}' in tag else tag
            if local_name in DIFF_FEATURE_TYPES:
                return local_name
        return ''

    @staticmethod
    def _extract_feature_element(member_elem: etree._Element, feature_type: str):
        """Feature要素を抽出"""
        for child in member_elem:
            tag = child.tag
            local_name = tag.split('}', 1)[1] if '}' in tag else tag
            if local_name == feature_type:
                return child
        return None