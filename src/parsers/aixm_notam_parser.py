import json
import os
import re
from lxml import etree
from typing import Dict, Any, List, Optional, Tuple
from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, GeometryCollection
from shapely.wkt import dumps as wkt_dumps

from src.parsers.schedule_utils import Timesheet

class AIXMNotamParser:
    """AIXM 5.1 / Digital NOTAM 改良版パーサー

    対応形式:
    1. 従来のNOTAM XML (series/number/year タグ + beginPosition/endPosition)
    2. Digital NOTAM (event:Event 内の event:textNOTAM + event:effectiveStart/effectiveEnd)
    """

    @staticmethod
    def _get_text_by_localname(root: etree.Element, local_name: str) -> Optional[str]:
        for elem in root.iter():
            if etree.QName(elem).localname.lower() == local_name.lower():
                if elem.text and elem.text.strip():
                    return elem.text.strip()
        return None

    @staticmethod
    def _get_text_by_localname_in_element(element: etree.Element, local_name: str) -> Optional[str]:
        """指定要素内のみからlocalnameでテキストを検索"""
        for elem in element.iter():
            if etree.QName(elem).localname.lower() == local_name.lower():
                if elem.text and elem.text.strip():
                    return elem.text.strip()
        return None

    @staticmethod
    def _extract_altitude_limits(root: etree.Element) -> 'Tuple[Optional[int], Optional[int]]':
        """XML から下限・上限高度を FT 整数で抽出する。

        優先順位:
        1. event:lowerLimit / event:upperLimit (例: "GND", "821FT AGL", "FL050")
        2. event:minimumFL / event:maximumFL  (例: "000", "050")
        3. aixm:lowerLimit / aixm:upperLimit  (uom="FT" 属性付き)

        戻り値: (lower_ft, upper_ft)  不明な場合は None
        """
        FL_TO_FT = 100  # 1 FL = 100 FT

        def parse_value(text: str, uom: str = '') -> Optional[int]:
            if not text:
                return None
            t = text.strip().upper()
            if t in ('GND', 'SFC', 'GROUND', 'SURFACE'):
                return 0
            m = re.match(r'^FL\s*(\d+)$', t)
            if m:
                return int(m.group(1)) * FL_TO_FT
            m = re.match(r'^(\d+)\s*FT', t)
            if m:
                return int(m.group(1))
            if re.match(r'^\d+$', t):
                val = int(t)
                if uom.upper() == 'FT':
                    return val
            return None

        lower_ft: Optional[int] = None
        upper_ft: Optional[int] = None
        lower_fl: Optional[int] = None
        upper_fl: Optional[int] = None
        aixm_lower: Optional[int] = None
        aixm_upper: Optional[int] = None

        for elem in root.iter():
            local = etree.QName(elem).localname.lower()
            ns = etree.QName(elem).namespace or ''
            is_event = 'event' in ns
            is_aixm = 'aixm' in ns

            if local == 'lowerlimit':
                if is_event and lower_ft is None:
                    lower_ft = parse_value(elem.text or '')
                elif is_aixm and aixm_lower is None:
                    uom = elem.get('uom', '')
                    v = parse_value(elem.text or '', uom)
                    if v is not None:
                        aixm_lower = v
            elif local == 'upperlimit':
                if is_event and upper_ft is None:
                    upper_ft = parse_value(elem.text or '')
                elif is_aixm and aixm_upper is None:
                    uom = elem.get('uom', '')
                    v = parse_value(elem.text or '', uom)
                    if v is not None:
                        aixm_upper = v
            elif local == 'minimumfl' and is_event and lower_fl is None:
                try:
                    lower_fl = int((elem.text or '').strip()) * FL_TO_FT
                except (ValueError, AttributeError):
                    pass
            elif local == 'maximumfl' and is_event and upper_fl is None:
                try:
                    upper_fl = int((elem.text or '').strip()) * FL_TO_FT
                except (ValueError, AttributeError):
                    pass

        result_lower = lower_ft if lower_ft is not None else (lower_fl if lower_fl is not None else aixm_lower)
        result_upper = upper_ft if upper_ft is not None else (upper_fl if upper_fl is not None else aixm_upper)
        return result_lower, result_upper

    @staticmethod
    def _extract_operational_status(root: etree.Element) -> str:
        """XML全体から aixm:TaxiwayElement 内の operationalStatus を抽出する

        Digital NOTAM では、影響を受ける誘導路のジオメトリが
        aixm:TaxiwayElement の aixm:ManoeuvringAreaAvailability 内に
        aixm:operationalStatus として記述されている。
        例: CLOSED, LIMITED, NORMAL
        """
        statuses = []
        for elem in root.iter():
            local = etree.QName(elem).localname.lower()
            if local == 'operationalstatus':
                if elem.text and elem.text.strip():
                    statuses.append(elem.text.strip().upper())
        # 最初に見つかったステータスを返す (CLOSED > LIMITED > NORMAL の優先順位)
        for priority in ['CLOSED', 'LIMITED', 'NORMAL']:
            if priority in statuses:
                return priority
        return 'NORMAL'

    @staticmethod
    def _clean_uuid(uuid_raw: str) -> str:
        """UUID形式を統一する（urn:uuid: や uuid. プレフィックスを除去）"""
        if not uuid_raw:
            return ""
        val = uuid_raw.strip()
        val = re.sub(r'^(urn:uuid:|uuid\.)', '', val, flags=re.IGNORECASE)
        return val

    @staticmethod
    def _extract_associated_aip_uuid(root: etree.Element) -> Optional[str]:
        """NOTAM XML から AIP 施設への UUID 参照を抽出し、カンマ区切りで返す。

        2つの参照パターンに対応する:

        パターン A — xlink:href 形式 (1対1 または 1対多参照):
          <aixm:associatedTaxiway xlink:href="urn:uuid:XXXX"/>
          → href から "urn:uuid:" / "uuid." プレフィックスを除去して純粋な UUID を抽出

        パターン B — TaxiwayElement gml:id 直接指定 (D項付きNOTAM、1対多):
          <aixm:TaxiwayElement gml:id="uuid.XXXX">
            <aixm:ManoeuvringAreaAvailability>
              <aixm:operationalStatus>CLOSED</aixm:operationalStatus>
          → TaxiwayElement/RunwayElement/ApronElement/AircraftStand の gml:id からプレフィックスを除去して使用

        パターン A が存在する場合はそちらを優先。なければパターン B を使用。
        複数施設が含まれる場合はカンマ区切りの文字列として返す。
        見つからない場合は None を返す。
        """
        XLINK_HREF = '{http://www.w3.org/1999/xlink}href'
        GML_ID = '{http://www.opengis.net/gml/3.2}id'
        ASSOC_LOCALS = {
            'associatedtaxiway',
            'associatedrunway',
            'associatedapron',
            'associatedaircraftstand',
        }
        FEATURE_LOCALS = {
            'taxiwayelement',
            'runwayelement',
            'apronelement',
            'aircraftstand',
        }

        # パターン A: xlink:href 参照を優先探索 (全件収集)
        uuids_a = []
        seen_a = set()
        for elem in root.iter():
            local = etree.QName(elem).localname.lower()
            if local in ASSOC_LOCALS:
                href = elem.get(XLINK_HREF, '').strip()
                if href:
                    clean = AIXMNotamParser._clean_uuid(href)
                    if clean and clean not in seen_a:
                        seen_a.add(clean)
                        uuids_a.append(clean)

        if uuids_a:
            return ','.join(uuids_a)

        # パターン B: フィーチャー要素の gml:id を直接収集 (D項付きNOTAM)
        uuids_b = []
        seen_b = set()
        for elem in root.iter():
            local = etree.QName(elem).localname.lower()
            if local in FEATURE_LOCALS:
                gml_id = elem.get(GML_ID, '').strip()
                if gml_id:
                    clean = AIXMNotamParser._clean_uuid(gml_id)
                    if clean and clean not in seen_b:
                        seen_b.add(clean)
                        uuids_b.append(clean)

        return ','.join(uuids_b) if uuids_b else None


    @staticmethod
    def parse_icao_coord(coord_str: str) -> Optional[Point]:
        try:
            m = re.match(r'(\d{2})(\d{2})([NS])(\d{3})(\d{2})([EW])', coord_str)
            if m:
                lat_deg, lat_min, lat_dir, lon_deg, lon_min, lon_dir = m.groups()
                lat = float(lat_deg) + float(lat_min) / 60.0
                if lat_dir == 'S': lat = -lat
                
                lon = float(lon_deg) + float(lon_min) / 60.0
                if lon_dir == 'W': lon = -lon
                
                return Point(lon, lat)
        except Exception:
            pass
        return None

    @staticmethod
    def parse_yyyymmddhhmm(value: str) -> Optional[str]:
        """YYMMDDHHMM 形式 (例: 2608131600) を ISO 8601 (YYYY-MM-DDTHH:MM:SSZ) に変換"""
        if not value:
            return None
        m = re.match(r'^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$', value.strip())
        if m:
            yy, mm, dd, hh, mi = m.groups()
            year = 2000 + int(yy)
            return f"{year:04d}-{mm}-{dd}T{hh}:{mi}:00Z"
        return None

    @staticmethod
    def _extract_timesheets(root: etree.Element) -> List[Timesheet]:
        """XML 内のすべての <aixm:Timesheet> 要素を解析して Timesheet リストを返す。

        対応フィールド:
            aixm:day         — 曜日コード ('MON'〜'SUN') / 'ANY' など
            aixm:startTime   — "HH:MM" 形式
            aixm:endTime     — "HH:MM" 形式
            aixm:excluded    — "YES" の場合 excluded=True
            aixm:timeReference が UTC 以外の場合も現時点では UTC として扱う
        """
        result: List[Timesheet] = []
        for ts_elem in root.iter():
            if etree.QName(ts_elem).localname != 'Timesheet':
                continue

            day: Optional[str] = None
            start_hh = start_mm = end_hh = end_mm = None
            excluded = False

            for child in ts_elem:
                local = etree.QName(child).localname
                text = (child.text or '').strip()

                if local == 'day':
                    day = text.upper() if text else None
                elif local == 'startTime':
                    m = re.match(r'^(\d{1,2}):(\d{2})$', text)
                    if m:
                        start_hh, start_mm = int(m.group(1)), int(m.group(2))
                elif local == 'endTime':
                    m = re.match(r'^(\d{1,2}):(\d{2})$', text)
                    if m:
                        end_hh, end_mm = int(m.group(1)), int(m.group(2))
                elif local == 'excluded':
                    excluded = text.upper() == 'YES'

            if start_hh is not None and end_hh is not None:
                result.append(Timesheet(
                    day=day,
                    start_hh=start_hh,
                    start_mm=start_mm if start_mm is not None else 0,
                    end_hh=end_hh,
                    end_mm=end_mm if end_mm is not None else 0,
                    excluded=excluded,
                ))

        return result

    @classmethod
    def parse_geometry(cls, root: etree.Element) -> Tuple[Optional[Any], str]:
        """
        GML精密座標(posList / pos)を全件ループ抽出し、なければQ項簡易座標(coordinates)にフォールバック。
        (geom_object, precision_type) を返す。
        複数要素が存在する場合は MultiLineString / MultiPolygon / MultiPoint / GeometryCollection を返す。
        """
        bbox_tags = {'boundedby', 'envelope'}
        surface_tags = {'polygonpatch', 'surface', 'elevatedsurface', 'polygon'}
        curve_tags = {'curve', 'linestring', 'geodesicstring', 'segments', 'curvemember'}

        geometries = []

        for child in root.iter():
            tag_local = etree.QName(child).localname.lower()
            if tag_local not in ('poslist', 'pos'):
                continue

            ancestors = [etree.QName(p).localname.lower() for p in child.iterancestors()]
            if any(b in ancestors for b in bbox_tags):
                continue

            text = (child.text or '').strip()
            if not text:
                continue

            if tag_local == 'poslist':
                try:
                    raw = [float(x) for x in text.split()]
                    if len(raw) >= 4:
                        coords = [(raw[i+1], raw[i]) for i in range(0, len(raw), 2)]
                        is_surface = any(s in ancestors for s in surface_tags)
                        is_curve = any(c in ancestors for c in curve_tags)

                        if is_surface:
                            if len(coords) >= 3:
                                if coords[0] != coords[-1]:
                                    coords.append(coords[0])
                                geometries.append(Polygon(coords))
                        elif is_curve:
                            if len(coords) >= 2:
                                geometries.append(LineString(coords))
                        else:
                            # 祖先タグから特定できない場合のフォールバック
                            if len(coords) >= 3 and coords[0] == coords[-1]:
                                geometries.append(Polygon(coords))
                            elif len(coords) >= 2:
                                geometries.append(LineString(coords))
                except Exception:
                    pass

            elif tag_local == 'pos':
                try:
                    raw = [float(x) for x in text.split()]
                    if len(raw) >= 2:
                        geometries.append(Point(raw[1], raw[0]))
                except Exception:
                    pass

        # 1. 精密GMLジオメトリが抽出できた場合
        if geometries:
            if len(geometries) == 1:
                geom = geometries[0]
                if isinstance(geom, Polygon):
                    return geom, 'PRECISE_POLYGON'
                elif isinstance(geom, LineString):
                    return geom, 'PRECISE_LINE'
                elif isinstance(geom, Point):
                    return geom, 'PRECISE_POINT'
                return geom, 'PRECISE_GEOMETRY'
            else:
                # 複数要素の統合
                all_poly = all(isinstance(g, Polygon) for g in geometries)
                all_line = all(isinstance(g, LineString) for g in geometries)
                all_point = all(isinstance(g, Point) for g in geometries)

                if all_poly:
                    return MultiPolygon(geometries), 'PRECISE_POLYGON'
                elif all_line:
                    return MultiLineString(geometries), 'PRECISE_LINE'
                elif all_point:
                    return MultiPoint(geometries), 'PRECISE_POINT'
                else:
                    return GeometryCollection(geometries), 'PRECISE_MIXED'

        # 2. 簡易Q項座標 (フォールバック)
        coord_text = cls._get_text_by_localname(root, 'coordinates')
        if coord_text:
            point = cls.parse_icao_coord(coord_text)
            if point:
                return point, 'APPROX_POINT'

        return None, 'NONE'

    @classmethod
    def _parse_digital_notam(cls, root: etree.Element, file_path: str, default_series: str, default_fir: str) -> Optional[Dict[str, Any]]:
        """Digital NOTAM形式 (event:Event 内の event:textNOTAM) をパース"""
        # event:Event 要素を探す
        event_elem = None
        for elem in root.iter():
            if etree.QName(elem).localname == 'Event':
                event_elem = elem
                break

        if event_elem is None:
            return None

        # event:textNOTAM 内の NOTAM 要素を探す
        notam_elem = None
        for elem in event_elem.iter():
            if etree.QName(elem).localname == 'NOTAM':
                notam_elem = elem
                break

        if notam_elem is None:
            return None

        # NOTAM ID 構築
        series = cls._get_text_by_localname_in_element(notam_elem, 'series') or default_series
        number = cls._get_text_by_localname_in_element(notam_elem, 'number')
        year = cls._get_text_by_localname_in_element(notam_elem, 'year')

        if series and number and year:
            short_year = year[-2:] if len(year) == 4 else year
            notam_id = f"{series}{number}/{short_year}"
        else:
            notam_id = os.path.basename(file_path).replace('.xml', '')

        # 場所・Qコード
        location = cls._get_text_by_localname_in_element(notam_elem, 'location')
        qcode = cls._get_text_by_localname_in_element(notam_elem, 'selectioncode')
        if qcode and not qcode.startswith('Q'):
            qcode = 'Q' + qcode

        # テキスト
        text_e = cls._get_text_by_localname_in_element(notam_elem, 'text') or ""

        # 有効期間 (YYMMDDHHMM 形式 → ISO 8601)
        start_raw = cls._get_text_by_localname_in_element(notam_elem, 'effectivestart')
        end_raw = cls._get_text_by_localname_in_element(notam_elem, 'effectiveend')
        start_time = cls.parse_yyyymmddhhmm(start_raw) if start_raw else None
        end_time = cls.parse_yyyymmddhhmm(end_raw) if end_raw else None

        # NOTAM Type (N / R / C)
        notam_type = 'N'
        ref_notam_id = None
        type_raw = cls._get_text_by_localname_in_element(notam_elem, 'type')
        if type_raw:
            type_upper = type_raw.strip().upper()
            if type_upper == 'C':
                notam_type = 'C'
            elif type_upper == 'R':
                notam_type = 'R'

        # キャンセル/置換対象の NOTAM ID 検索
        ref_match = re.search(r'([A-Z]\d{4}/\d{2})', text_e)
        if ref_match and ref_match.group(1) != notam_id:
            ref_notam_id = ref_match.group(1)

        # ジオメトリ抽出
        # 1. GML精密座標 (gml:posList) を優先抽出
        geom, precision_type = cls.parse_geometry(root)

        # 2. 精密ジオメトリが無ければ Q項座標 にフォールバック
        if geom is None:
            coord_text = cls._get_text_by_localname_in_element(notam_elem, 'coordinates')
            if coord_text:
                point = cls.parse_icao_coord(coord_text)
                if point:
                    geom = point
                    precision_type = 'APPROX_POINT'

        # シナリオ情報 (TWY.CLS/LIM.JP など)
        scenario = cls._get_text_by_localname_in_element(event_elem, 'scenario') or ""

        # AIXM Timesheet スケジュール情報 (優先)
        timesheets = cls._extract_timesheets(root)

        # D項スケジュール情報 (フォールバック: AIXM Timesheet がない場合のみ使用)
        schedule = cls._get_text_by_localname_in_element(notam_elem, 'schedule') or ""

        # 運用ステータス抽出 (CLOSED / LIMITED / NORMAL)
        operational_status = cls._extract_operational_status(root)

        # AIP 施設への UUID 参照抽出
        associated_aip_gml_id = cls._extract_associated_aip_uuid(root)

        # 高度情報抽出
        lower_limit_ft, upper_limit_ft = cls._extract_altitude_limits(root)

        wkt_geom = wkt_dumps(geom) if geom else None

        # Timesheet を JSON 文字列にシリアライズして保存
        timesheets_json = json.dumps([
            {
                'day': ts.day,
                'start_hh': ts.start_hh,
                'start_mm': ts.start_mm,
                'end_hh': ts.end_hh,
                'end_mm': ts.end_mm,
                'excluded': ts.excluded,
            }
            for ts in timesheets
        ]) if timesheets else None

        return {
            'notam_id': notam_id,
            'fir': default_fir,
            'series': series,
            'location': location,
            'q_code': qcode,
            'notam_type': notam_type,
            'ref_notam_id': ref_notam_id,
            'status': 'ACTIVE',
            'precision_type': precision_type,
            'operational_status': operational_status,
            'effective_start': start_time,
            'effective_end': end_time,
            'text': text_e,
            'geometry_wkt': wkt_geom,
            'file_path': file_path,
            'scenario': scenario,
            'schedule': schedule,
            'timesheets_json': timesheets_json,
            'associated_aip_gml_id': associated_aip_gml_id,
            'lower_limit_ft': lower_limit_ft,
            'upper_limit_ft': upper_limit_ft,
        }

    @classmethod
    def parse_file(cls, file_path: str, default_series: str, default_fir: str) -> Optional[Dict[str, Any]]:
        try:
            tree = etree.parse(file_path)
            root = tree.getroot()
        except Exception as e:
            print(f"⚠️ XML読み込みエラー ({file_path}): {e}")
            return None

        # 1. Digital NOTAM形式を優先的に試す
        digital_result = cls._parse_digital_notam(root, file_path, default_series, default_fir)
        if digital_result:
            return digital_result

        # 2. 従来のNOTAM形式にフォールバック
        series = cls._get_text_by_localname(root, 'series') or default_series
        number = cls._get_text_by_localname(root, 'number')
        year = cls._get_text_by_localname(root, 'year')

        if series and number and year:
            short_year = year[-2:] if len(year) == 4 else year
            notam_id = f"{series}{number}/{short_year}"
        else:
            notam_id = os.path.basename(file_path).replace('.xml', '')

        location = cls._get_text_by_localname(root, 'location')
        qcode = cls._get_text_by_localname(root, 'selectioncode')
        if qcode and not qcode.startswith('Q'):
            qcode = 'Q' + qcode
        if not qcode:
            qcode = cls._get_text_by_localname(root, 'qcode') or cls._get_text_by_localname(root, 'codeq')

        text_e = cls._get_text_by_localname(root, 'text') or cls._get_text_by_localname(root, 'iteme') or ""

        start_time = cls._get_text_by_localname(root, 'beginposition') or cls._get_text_by_localname(root, 'effectivestart')
        end_time = cls._get_text_by_localname(root, 'endposition') or cls._get_text_by_localname(root, 'effectiveend')

        # --- NOTAM Type (N / R / C) 及び キャンセル対象IDの抽出 ---
        notam_type = 'N'  # デフォルトは NEW
        ref_notam_id = None

        # XMLタグやテキストから CNL / RPL の判定
        if "NOTAM CNL" in text_e.upper() or (qcode and qcode.endswith("AK")):
            notam_type = 'C'
        elif "NOTAMR" in text_e.upper() or (qcode and qcode.endswith("AL")):
            notam_type = 'R'

        # キャンセル/置換対象の NOTAM ID 検索 (例: "NOTAM CNL A1156/26" や XML参照タグから)
        ref_match = re.search(r'([A-Z]\d{4}/\d{2})', text_e)
        if ref_match and ref_match.group(1) != notam_id:
            ref_notam_id = ref_match.group(1)

        # ジオメトリ抽出と精度分類
        geom, precision_type = cls.parse_geometry(root)
        wkt_geom = wkt_dumps(geom) if geom else None

        # 運用ステータス抽出
        operational_status = cls._extract_operational_status(root)

        # AIP 施設への UUID 参照抽出
        associated_aip_gml_id = cls._extract_associated_aip_uuid(root)

        # 高度情報抽出
        lower_limit_ft, upper_limit_ft = cls._extract_altitude_limits(root)

        return {
            'notam_id': notam_id,
            'fir': default_fir,
            'series': series,
            'location': location,
            'q_code': qcode,
            'notam_type': notam_type,      # 'N', 'R', 'C'
            'ref_notam_id': ref_notam_id,  # キャンセル・置換対象のID
            'status': 'ACTIVE',            # 初期値
            'precision_type': precision_type, # 'PRECISE_POLYGON', 'APPROX_POINT' 等
            'operational_status': operational_status,
            'effective_start': start_time,
            'effective_end': end_time,
            'text': text_e,
            'geometry_wkt': wkt_geom,
            'file_path': file_path,
            'scenario': "",
            'schedule': "",
            'timesheets_json': None,
            'associated_aip_gml_id': associated_aip_gml_id,
            'lower_limit_ft': lower_limit_ft,
            'upper_limit_ft': upper_limit_ft,
        }