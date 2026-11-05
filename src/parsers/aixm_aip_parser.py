"""
AIXM 5.1 AIPデータパーサー（仕様書 3.1, 3.2, 4.1 完全対応版）

- Pass 0: lxml.etree.iterparse による800MB級XMLのストリーミング分解
- Pass 1: 全Featureの要素抽出一括登録 (gml_id, feature_type, name, valid_from/valid_to, geojson)
- Pass 2: 汎用 xlink:href 解析 & parent_id 結合
"""

import os
import sys
import re
import json
from datetime import datetime, timezone
from lxml import etree
from collections import defaultdict
from typing import Iterator, Optional

from config.settings import RAW_AIP_SPLIT_DIR

# Windows cp932対策
sys.stdout.reconfigure(encoding='utf-8')

# AIXM名前空間
NS_AIXM = "http://www.aixm.aero/schema/5.1.1"
NS_GML = "http://www.opengis.net/gml/3.2"
NS_XLINK = "http://www.w3.org/1999/xlink"
NS_MESSAGE = "http://www.aixm.aero/schema/5.1.1/message"

NS = {
    'aixm': NS_AIXM,
    'gml': NS_GML,
    'xlink': NS_XLINK,
    'message': NS_MESSAGE,
}

# 対象Feature種別
TARGET_FEATURES = [
    'AirportHeliport', 'Runway', 'RunwayDirection', 'RunwayElement',
    'Taxiway', 'TaxiwayElement', 'Apron', 'ApronElement', 'AircraftStand',
    'Airspace', 'Navaid', 'Obstacle', 'DesignatedPoint', 'Route', 'RoutePiece',
    'HoldingPattern', 'StandardInstrumentArrival', 'StandardInstrumentDeparture'
]

# 親子関係の優先リンク定義 (仕様書 3.2 適用例)
PARENT_TAG_MAP = {
    'RunwayElement': ['associatedRunway'],
    'RunwayDirection': ['usedRunway'],
    'TaxiwayElement': ['associatedTaxiway'],
    'ApronElement': ['associatedApron'],
    'AirspaceVolume': ['hostAirspace'],
    'RouteSegment': ['associatedRoute'],
    'RoutePiece': ['associatedRoute'],
}


class AIXMAipParser:
    """AIXM 5.1.1 AIPデータパーサー 仕様書(3.1, 3.2, 4.1)完全対応版"""

    # ============================================================
    # Pass 0: ストリーミング処理 (仕様書 3.1)
    # ============================================================

    @staticmethod
    def stream_features(xml_path: str) -> Iterator[etree._Element]:
        """
        lxml.etree.iterparse によるストリーミング分解 (Pass 0)。
        800MB規模のXMLをメモリ不足(OOM)を起こさずに処理するため、
        Featureノードを逐次抽出し、メモリを随時解放する。

        Yields:
            各AIXM Featureノード (aixm:AirportHeliport, aixm:Airspace 等)
        """
        print(f"🚀 Pass 0: ストリーミング解析開始: {xml_path}")
        context = etree.iterparse(
            xml_path,
            events=('end',),
            huge_tree=True,
            recover=True,
        )

        feature_count = 0
        for event, elem in context:
            tag = elem.tag
            local_name = tag.split('}', 1)[1] if '}' in tag else tag

            if local_name in TARGET_FEATURES:
                feature_count += 1
                if feature_count % 10000 == 0:
                    print(f"  Pass 0: {feature_count} Feature抽出済み...")
                yield elem

                # Feature処理完了後にのみ子要素・メモリを解放
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]

        print(f"✅ Pass 0: ストリーミング解析完了 ({feature_count} Feature)")


    @staticmethod
    def split_features(xml_path: str) -> dict:
        """
        巨大なAIXM XMLファイルをFeature種別ごとに分割する (Pass 0 の補助)。
        分割ファイルは data/raw/aip/split/{FeatureType}.xml に保存される。
        """
        print(f"🚀 AIP XMLストリーミング分割開始: {xml_path}")
        os.makedirs(RAW_AIP_SPLIT_DIR, exist_ok=True)

        feature_counts = defaultdict(int)
        writers = {}

        header = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<AIXM-Snapshot '
            f'xmlns:message="{NS_MESSAGE}" '
            f'xmlns:aixm="{NS_AIXM}" '
            f'xmlns:gml="{NS_GML}" '
            f'xmlns:xlink="{NS_XLINK}" '
            'xmlns:gmd="http://www.isotc211.org/2005/gmd" '
            'xmlns:gco="http://www.isotc211.org/2005/gco" '
            'xmlns:gss="http://www.isotc211.org/2005/gss" '
            'xmlns:gsr="http://www.isotc211.org/2005/gsr" '
            'xmlns:gts="http://www.isotc211.org/2005/gts" '
            'xmlns:xml="http://www.w3.org/XML/1998/namespace" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        )

        try:
            for elem in AIXMAipParser.stream_features(xml_path):
                local_name = etree.QName(elem).localname
                feature_counts[local_name] += 1

                if local_name not in writers:
                    split_file_path = RAW_AIP_SPLIT_DIR / f"{local_name}.xml"
                    writers[local_name] = open(split_file_path, 'w', encoding='utf-8')
                    writers[local_name].write(header)

                writers[local_name].write(
                    etree.tostring(elem, encoding='unicode', pretty_print=False)
                )

        except Exception as e:
            print(f"❌ 分割処理エラー: {e}")
        finally:
            for f in writers.values():
                f.write('</AIXM-Snapshot>\n')
                f.close()

        print("✅ AIP XML分割完了")
        return dict(feature_counts)

    # ============================================================
    # 共通ユーティリティ
    # ============================================================

    @staticmethod
    def _clean_uuid(uuid_raw: str) -> str:
        """UUID形式を統一する"""
        if not uuid_raw:
            return ""
        val = uuid_raw.strip()
        val = re.sub(r'^(urn:uuid:|uuid\.)', '', val, flags=re.IGNORECASE)
        return val

    @staticmethod
    def _parse_datetime(text: str) -> Optional[datetime]:
        """ISO8601日時文字列をUTC naive datetimeに変換"""
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            return None

    @staticmethod
    def _extract_name(elem: etree._Element, feature_type: str) -> str:
        """施設名/識別子を抽出"""
        if feature_type in ('TaxiwayElement', 'RunwayElement', 'ApronElement'):
            priority = ('note', 'designator', 'name')
        else:
            priority = ('designator', 'name', 'note')

        found: dict[str, str] = {}
        for child in elem.iter():
            ctag = child.tag
            local = ctag.split('}', 1)[1] if '}' in ctag else ctag
            key = local.lower()
            if key not in found and child.text and child.text.strip():
                if key in ('note', 'designator') or key.endswith('name'):
                    found[key] = child.text.strip()

        for tag in priority:
            if tag == 'name':
                for k, v in found.items():
                    if k.endswith('name'):
                        return v
            elif tag in found:
                return found[tag]

        return ""

    # ============================================================
    # Pass 2: 汎用 xlink:href 解析 & parent_id 結合 (仕様書 3.2)
    # ============================================================

    @staticmethod
    def _extract_parent_id(elem: etree._Element, feature_type: str) -> str:
        """
        汎用 xlink:href 解析により親Featureのgml_id (UUID) を抽出 (Pass 2)。
        特定のFeature種別に限定せず、すべてのAIXM要素に対して汎用的に適用する。
        """
        # 1. 優先リンク定義から検索
        if feature_type in PARENT_TAG_MAP:
            for tag in PARENT_TAG_MAP[feature_type]:
                # lxmlのiter()で直接検索 (XPathの@*を使わない安全な方法)
                for child in elem.iter(f'{{{NS_AIXM}}}{tag}'):
                    for attr_name, attr_val in child.attrib.items():
                        if 'href' in attr_name:
                            clean = AIXMAipParser._clean_uuid(attr_val)
                            if clean:
                                return clean

        # 2. 汎用 xlink:href 検索 (urn:uuid:...)
        for child in elem.iter():
            for attr_name, attr_val in child.attrib.items():
                if 'href' in attr_name and 'urn:uuid:' in attr_val:
                    clean = AIXMAipParser._clean_uuid(attr_val)
                    if clean:
                        return clean

        return ""

    # ============================================================
    # Pass 1: 全Featureの要素抽出一括登録 (仕様書 3.2)
    # ============================================================

    @staticmethod
    def _extract_geometry_geojson(elem: etree._Element) -> dict | None:
        """要素からGMLジオメトリを抽出してGeoJSON dict構造を返す"""
        try:
            # 1. gml:pos (Point)
            points = elem.xpath('.//gml:pos', namespaces=NS)
            if points:
                for pt in points:
                    if pt.text:
                        coords = pt.text.strip().split()
                        if len(coords) >= 2:
                            try:
                                lat, lon = float(coords[0]), float(coords[1])
                                return {"type": "Point", "coordinates": [lon, lat]}
                            except ValueError:
                                continue

            # 2. gml:posList (Polygon / LineString)
            pos_lists = elem.xpath('.//gml:posList', namespaces=NS)
            for pl in pos_lists:
                if not pl.text:
                    continue
                coords_text = pl.text.strip().split()
                if len(coords_text) < 4:
                    continue
                coords = []
                try:
                    for i in range(0, len(coords_text) - 1, 2):
                        lat, lon = float(coords_text[i]), float(coords_text[i + 1])
                        coords.append([lon, lat])
                except ValueError:
                    continue

                if len(coords) >= 4:
                    is_poly = False
                    parent = pl.getparent()
                    while parent is not None:
                        ptag = parent.tag
                        if 'Polygon' in ptag or 'Surface' in ptag or 'LinearRing' in ptag:
                            is_poly = True
                            break
                        parent = parent.getparent()

                    if is_poly or (coords[0] == coords[-1] and len(coords) >= 4):
                        return {"type": "Polygon", "coordinates": [coords]}

                    if len(coords) >= 2:
                        return {"type": "LineString", "coordinates": coords}

        except Exception:
            pass
        return None

    @staticmethod
    def _extract_properties_json(elem: etree._Element) -> str:
        """その他の詳細属性パラメータをJSON化"""
        props = {}
        for child in elem:
            tag = child.tag.split('}', 1)[1] if '}' in child.tag else child.tag
            if tag in ('timeSlice', 'validTime', 'featureLifetime', 'interpretation',
                       'sequenceNumber', 'correctionNumber'):
                continue
            if child.text and child.text.strip():
                props[tag] = child.text.strip()
        return json.dumps(props, ensure_ascii=False) if props else "{}"

    @staticmethod
    def _extract_timeslice_info(elem: etree._Element) -> dict:
        """valid_from / valid_to および TimeSlice 情報を抽出"""
        info = {
            'valid_from': None,
            'valid_to': None,
            'interpretation': 'BASELINE',
            'sequence_number': 0,
            'correction_number': 0,
            'feature_lifetime_start': None,
            'feature_lifetime_end': None
        }

        try:
            valid_time_elem = elem.find('.//gml:validTime', namespaces=NS)
            if valid_time_elem is not None:
                valid_time = valid_time_elem.find('gml:TimePeriod', namespaces=NS)
                if valid_time is not None:
                    begin = valid_time.find('gml:beginPosition', namespaces=NS)
                    end = valid_time.find('gml:endPosition', namespaces=NS)

                    if begin is not None and begin.text:
                        info['valid_from'] = AIXMAipParser._parse_datetime(begin.text)

                    if end is not None:
                        if end.text:
                            info['valid_to'] = AIXMAipParser._parse_datetime(end.text)
                        elif end.get('indeterminatePosition') == 'unknown':
                            info['valid_to'] = None
                else:
                    time_instant = valid_time_elem.find('gml:TimeInstant', namespaces=NS)
                    if time_instant is not None:
                        time_pos = time_instant.find('gml:timePosition', namespaces=NS)
                        if time_pos is not None and time_pos.text:
                            info['valid_from'] = AIXMAipParser._parse_datetime(time_pos.text)
                            info['valid_to'] = None

            interp = elem.find('.//aixm:interpretation', namespaces=NS)
            if interp is not None and interp.text:
                info['interpretation'] = interp.text.strip()

            seq = elem.find('.//aixm:sequenceNumber', namespaces=NS)
            if seq is not None and seq.text:
                try:
                    info['sequence_number'] = int(seq.text.strip())
                except Exception:
                    pass

            corr = elem.find('.//aixm:correctionNumber', namespaces=NS)
            if corr is not None and corr.text:
                try:
                    info['correction_number'] = int(corr.text.strip())
                except Exception:
                    pass

            lifetime = elem.find('.//aixm:featureLifetime', namespaces=NS)
            if lifetime is not None:
                period = lifetime.find('gml:TimePeriod', namespaces=NS)
                if period is not None:
                    begin = period.find('gml:beginPosition', namespaces=NS)
                    end = period.find('gml:endPosition', namespaces=NS)

                    if begin is not None and begin.text:
                        info['feature_lifetime_start'] = AIXMAipParser._parse_datetime(begin.text)

                    if end is not None and end.text:
                        info['feature_lifetime_end'] = AIXMAipParser._parse_datetime(end.text)

            if info['valid_to'] is None and info['feature_lifetime_end'] is not None:
                info['valid_to'] = info['feature_lifetime_end']

        except Exception as e:
            print(f"⚠️ TimeSlice抽出エラー: {e}")

        return info

    @staticmethod
    def extract_feature_record(elem: etree._Element, feature_type: str) -> dict:
        """
        Feature要素から仕様書 4.1 スキーマ準拠のレコードを抽出 (Pass 1)。
        """
        # gml_id 抽出
        gml_id = ""
        for attr_name, attr_val in elem.attrib.items():
            if 'id' in attr_name:
                gml_id = AIXMAipParser._clean_uuid(attr_val)
                break
        if not gml_id:
            gml_id = AIXMAipParser._clean_uuid(elem.attrib.get('id', ''))

        # TimeSlice ごとにレコード生成
        records = []
        timeslices = elem.findall(f'.//{{{NS_AIXM}}}timeSlice')

        for timeslice_elem in timeslices:
            name = AIXMAipParser._extract_name(timeslice_elem, feature_type)
            parent_id = AIXMAipParser._extract_parent_id(timeslice_elem, feature_type)

            geom_dict = AIXMAipParser._extract_geometry_geojson(timeslice_elem)
            geojson_str = json.dumps(geom_dict) if geom_dict else None

            timeslice_info = AIXMAipParser._extract_timeslice_info(timeslice_elem)
            properties_json = AIXMAipParser._extract_properties_json(timeslice_elem)

            records.append({
                "gml_id": gml_id,
                "feature_type": feature_type,
                "name": name,
                "geojson": geojson_str,
                "parent_id": parent_id,
                "valid_from": timeslice_info['valid_from'],
                "valid_to": timeslice_info['valid_to'],
                "properties_json": properties_json,
                # 履歴・差分制御補助フィールド
                "interpretation": timeslice_info['interpretation'],
                "sequence_number": timeslice_info['sequence_number'],
                "correction_number": timeslice_info['correction_number'],
                "raw_xml": etree.tostring(timeslice_elem, encoding='utf-8').decode('utf-8')
            })

        return records

    @staticmethod
    def extract_feature_record_from_timeslice(
        feature_elem: etree._Element,
        timeslice_elem: etree._Element,
        feature_type: str
    ) -> dict | None:
        """
        指定したタイムスライスからFeatureレコードを抽出 (DIFF用)。

        Args:
            feature_elem: Feature要素 (gml_id取得用)
            timeslice_elem: タイムスライス要素 (属性・幾何・期間取得用)
            feature_type: Feature種別

        Returns:
            仕様書 4.1 スキーマ準拠のレコード、または None
        """
        try:
            # gml_id 抽出
            gml_id = ""
            for attr_name, attr_val in feature_elem.attrib.items():
                if 'id' in attr_name:
                    gml_id = AIXMAipParser._clean_uuid(attr_val)
                    break
            if not gml_id:
                return None

            name = AIXMAipParser._extract_name(timeslice_elem, feature_type)
            parent_id = AIXMAipParser._extract_parent_id(timeslice_elem, feature_type)

            geom_dict = AIXMAipParser._extract_geometry_geojson(timeslice_elem)
            geojson_str = json.dumps(geom_dict) if geom_dict else None

            timeslice_info = AIXMAipParser._extract_timeslice_info(timeslice_elem)
            properties_json = AIXMAipParser._extract_properties_json(timeslice_elem)

            return {
                "gml_id": gml_id,
                "feature_type": feature_type,
                "name": name,
                "geojson": geojson_str,
                "parent_id": parent_id,
                "valid_from": timeslice_info['valid_from'],
                "valid_to": timeslice_info['valid_to'],
                "properties_json": properties_json,
                # 履歴・差分制御補助フィールド
                "interpretation": timeslice_info['interpretation'],
                "sequence_number": timeslice_info['sequence_number'],
                "correction_number": timeslice_info['correction_number'],
                "raw_xml": etree.tostring(timeslice_elem, encoding='utf-8').decode('utf-8')
            }
        except Exception:
            return None

    @staticmethod
    def parse_feature_file(file_path: str, feature_type: str,
                           filter_time: datetime = None) -> list:
        """
        分割XMLファイルをパースして仕様書準拠レコードリストを返す (Pass 1)。
        """
        records = []
        if not os.path.exists(file_path):
            return records

        try:
            context = etree.iterparse(file_path, events=('end',), tag=etree.Element)
            for event, elem in context:
                tag = elem.tag
                local_name = tag.split('}', 1)[1] if '}' in tag else tag

                if local_name == feature_type:
                    feature_records = AIXMAipParser.extract_feature_record(elem, feature_type)

                    for record in feature_records:
                        if filter_time is not None:
                            valid_from = record['valid_from']
                            valid_to = record['valid_to']
                            if valid_from and filter_time < valid_from:
                                continue
                            if valid_to and filter_time > valid_to:
                                continue
                        records.append(record)

                    elem.clear()

        except Exception as e:
            print(f"⚠️ パースエラー ({feature_type}): {e}")

        return records