import os
import sys
import json
import math
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
import duckdb
from fastapi import FastAPI, Query, Response, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from config.settings import NOTAM_DB_PATH, AIP_DB_PATH, AIP_ARCHIVE_DB_PATH, WEB_HOST, WEB_PORT, DEBUG_USE_ARCHIVE_DB
from src.receiver.swim_api_client import SWIMAPIClient

# ログ設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from parsers.schedule_utils import (
    Timesheet,
    is_schedule_active,
    is_schedule_active_from_timesheets,
)

# SWIM WebAPI クライアントのインスタンス
swim_client = SWIMAPIClient()

NOTAM_DB = str(NOTAM_DB_PATH)

if DEBUG_USE_ARCHIVE_DB:
    AIP_DB = str(AIP_ARCHIVE_DB_PATH)
else:
    AIP_DB = str(AIP_DB_PATH)

JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """サーバー起動時にデータベース接続を初期化し使い回す"""
    logger.info(f"=== データベース接続初期化 ===")
    logger.info(f"デバッグモード (アーカイブDB使用): {DEBUG_USE_ARCHIVE_DB}")
    logger.info(f"AIP DB path: {AIP_DB}")
    logger.info(f"NOTAM DB path: {NOTAM_DB}")

    app.state.aip_con = None
    app.state.notam_con = None

    if os.path.exists(AIP_DB):
        try:
            size_mb = os.path.getsize(AIP_DB) / (1024 * 1024)
            logger.info(f"✅ AIPデータベースが見つかりました: {AIP_DB} ({size_mb:.1f} MB)")
            con = duckdb.connect(AIP_DB, read_only=True)
            count = con.execute("SELECT COUNT(*) FROM aip_features").fetchone()[0]
            logger.info(f"   aip_featuresテーブル: {count} 件のレコード")
            app.state.aip_con = con
        except Exception as e:
            logger.error(f"   AIPデータベースの読み込みに失敗: {e}")
    else:
        logger.error(f"❌ AIPデータベースが見つかりません: {AIP_DB}")

    if os.path.exists(NOTAM_DB):
        try:
            size_mb = os.path.getsize(NOTAM_DB) / (1024 * 1024)
            logger.info(f"✅ NOTAMデータベースが見つかりました: {NOTAM_DB} ({size_mb:.1f} MB)")
            con = duckdb.connect(NOTAM_DB, read_only=True)
            count = con.execute("SELECT COUNT(*) FROM notams").fetchone()[0]
            logger.info(f"   notamsテーブル: {count} 件のレコード")
            app.state.notam_con = con
        except Exception as e:
            logger.error(f"   NOTAMデータベースの読み込みに失敗: {e}")
    else:
        logger.error(f"❌ NOTAMデータベースが見つかりません: {NOTAM_DB}")

    yield

    if getattr(app.state, "aip_con", None):
        try:
            app.state.aip_con.close()
        except Exception:
            pass
    if getattr(app.state, "notam_con", None):
        try:
            app.state.notam_con.close()
        except Exception:
            pass
    logger.info("Database connections closed.")


app = FastAPI(title="AIP/NOTAM 可視化システム", lifespan=lifespan)

app.add_middleware(GZipMiddleware, minimum_size=1000)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def read_root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Index.html not found</h1>", status_code=404)


def parse_jst_datetime(value: str) -> datetime | None:
    """JSTローカル時刻文字列 (YYYY-MM-DD HH:MM 形式) を aware UTC datetime に変換"""
    if not value:
        return None
    try:
        dt_naive = datetime.strptime(value.strip().replace("T", " "), "%Y-%m-%d %H:%M")
        return dt_naive.replace(tzinfo=JST).astimezone(UTC)
    except ValueError:
        return None


def _parse_timesheets_json(timesheets_json: str) -> list[Timesheet]:
    try:
        items = json.loads(timesheets_json)
        return [
            Timesheet(
                day=item.get('day'),
                start_hh=item['start_hh'],
                start_mm=item['start_mm'],
                end_hh=item['end_hh'],
                end_mm=item['end_mm'],
                excluded=item.get('excluded', False),
            )
            for item in items
        ]
    except Exception:
        return []


def _build_notam_status_map(request: Request, sel_start_utc, sel_end_utc) -> dict:
    con = getattr(request.app.state, "notam_con", None)
    if not con:
        return {}

    cursor = con.cursor()

    query = """
        SELECT associated_aip_gml_id, operational_status, schedule, timesheets_json, effective_start, effective_end
        FROM notams
        WHERE status = 'ACTIVE'
          AND associated_aip_gml_id IS NOT NULL
    """
    params = []

    if sel_start_utc is not None and sel_end_utc is not None:
        query += " AND effective_start IS NOT NULL AND effective_end IS NOT NULL"
        query += " AND effective_start < ? AND effective_end > ?"
        params.extend([sel_end_utc, sel_start_utc])
    elif sel_start_utc is not None:
        query += " AND effective_end IS NOT NULL AND effective_end >= ?"
        params.append(sel_start_utc)
    elif sel_end_utc is not None:
        query += " AND effective_start IS NOT NULL AND effective_start <= ?"
        params.append(sel_end_utc)

    rows = cursor.execute(query, params).fetchall()
    cursor.close()

    PRIORITY = {'CLOSED': 0, 'LIMITED': 1, 'NORMAL': 2}
    status_map: dict[str, str] = {}
    for raw_ids, op_status, schedule, timesheets_json, eff_start_raw, eff_end_raw in rows:
        if sel_start_utc is not None and sel_end_utc is not None and eff_start_raw and eff_end_raw:
            if eff_start_raw.tzinfo is None:
                eff_start_utc = eff_start_raw.replace(tzinfo=UTC)
            else:
                eff_start_utc = eff_start_raw.astimezone(UTC)

            if eff_end_raw.tzinfo is None:
                eff_end_utc = eff_end_raw.replace(tzinfo=UTC)
            else:
                eff_end_utc = eff_end_raw.astimezone(UTC)

            if timesheets_json:
                timesheets = _parse_timesheets_json(timesheets_json)
                if not is_schedule_active_from_timesheets(timesheets, sel_start_utc, sel_end_utc):
                    continue
            elif schedule:
                if not is_schedule_active(schedule, eff_start_utc, eff_end_utc, sel_start_utc, sel_end_utc):
                    continue

        op_status = (op_status or 'NORMAL').upper()
        for gml_id in raw_ids.split(','):
            gml_id = gml_id.strip().replace('urn:uuid:', '').replace('uuid.', '')
            if not gml_id:
                continue
            if gml_id not in status_map:
                status_map[gml_id] = op_status
            elif PRIORITY.get(op_status, 99) < PRIORITY.get(status_map[gml_id], 99):
                status_map[gml_id] = op_status

    return status_map


def _fetch_aip_geojsons(request: Request, gml_ids: list[str]) -> list[dict]:
    con = getattr(request.app.state, "aip_con", None)
    clean_ids = [g.strip().replace('urn:uuid:', '').replace('uuid.', '') for g in gml_ids if g.strip()]
    if not clean_ids or not con:
        return []

    cursor = con.cursor()
    placeholders = ",".join(["?"] * len(clean_ids))
    rows = cursor.execute(
        f"SELECT geojson, feature_type FROM aip_features WHERE gml_id IN ({placeholders}) AND geojson IS NOT NULL",
        clean_ids,
    ).fetchall()
    cursor.close()

    result = []
    for (geojson_str, feature_type) in rows:
        try:
            geom = json.loads(geojson_str)
            result.append({"geometry": geom, "feature_type": feature_type})
        except Exception:
            pass
    return result



def _extract_display_name(full_name: str) -> str:
    if not full_name:
        return full_name

    if '(' in full_name:
        return full_name.split('(')[0].strip()
    return full_name


def _compute_runway_endpoint(polygon_geojson: dict, direction_name: str) -> dict | None:
    if not polygon_geojson or polygon_geojson.get('type') != 'Polygon':
        return None

    coords = polygon_geojson['coordinates'][0]
    if len(coords) < 4:
        return None

    # 重複する最終頂点を除外した頂点リスト
    pts = coords[:-1] if coords[0] == coords[-1] else coords
    if len(pts) < 4:
        return None

    cx = sum(c[0] for c in pts) / len(pts)
    cy = sum(c[1] for c in pts) / len(pts)

    def _bearing(lon1, lat1, lon2, lat2):
        lon1_r, lat1_r, lon2_r, lat2_r = map(math.radians, [lon1, lat1, lon2, lat2])
        dlon = lon2_r - lon1_r
        x = math.sin(dlon) * math.cos(lat2_r)
        y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
        return (math.degrees(math.atan2(x, y)) + 360) % 360

    # 4頂点ポリゴンの隣接する各辺の長さを計算
    n = len(pts)
    edges = []
    for i in range(n):
        p1 = pts[i]
        p2 = pts[(i + 1) % n]
        dist = math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)
        mid = [(p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0]
        edges.append({'p1': p1, 'p2': p2, 'dist': dist, 'mid': mid})

    # 短辺2本を取得 (滑走路の両端 Threshold)
    edges.sort(key=lambda e: e['dist'])
    short_edge1 = edges[0]
    short_edge2 = edges[1]

    # 各短辺の中点の方位角（滑走路中心から短辺中点への方向）
    b1 = _bearing(cx, cy, short_edge1['mid'][0], short_edge1['mid'][1])
    b2 = _bearing(cx, cy, short_edge2['mid'][0], short_edge2['mid'][1])

    import re
    m = re.match(r'(\d{2})', direction_name)
    if not m:
        return None
    target_bearing = int(m.group(1)) * 10
    approach_side_bearing = (target_bearing + 180) % 360

    diff1 = min(abs(b1 - approach_side_bearing), 360 - abs(b1 - approach_side_bearing))
    diff2 = min(abs(b2 - approach_side_bearing), 360 - abs(b2 - approach_side_bearing))

    chosen_mid = short_edge1['mid'] if diff1 <= diff2 else short_edge2['mid']

    # 滑走路末端の短辺中央から滑走路中心線に沿って内側（約7%）に配置（滑走路番号ペイント位置）
    offset_x = chosen_mid[0] + (cx - chosen_mid[0]) * 0.07
    offset_y = chosen_mid[1] + (cy - chosen_mid[1]) * 0.07

    return {"type": "Point", "coordinates": [offset_x, offset_y]}


@app.get("/api/notams")
def get_notams(
    request: Request,
    start_time: str = Query(None, description="フィルター開始日時 (JST, YYYY-MM-DD HH:MM)"),
    end_time: str = Query(None, description="フィルター終了日時 (JST, YYYY-MM-DD HH:MM)"),
    alt_lower: int = Query(None, description="高度下限フィルター (FT)"),
    alt_upper: int = Query(None, description="高度上限フィルター (FT)"),
):
    """DuckDBからステータスがACTIVEなNOTAMのみをGeoJSON形式で抽出。"""
    con = getattr(request.app.state, "notam_con", None)
    if not con:
        return JSONResponse(content={"type": "FeatureCollection", "features": []})

    sel_start_utc = parse_jst_datetime(start_time) if start_time else None
    sel_end_utc = parse_jst_datetime(end_time) if end_time else None

    if sel_end_utc is not None:
        sel_end_utc = sel_end_utc + timedelta(minutes=1)

    query = """
        SELECT
            notam_id,
            series,
            location,
            q_code,
            precision_type,
            operational_status,
            strftime(effective_start, '%Y-%m-%d %H:%M') AS start_time,
            strftime(effective_end, '%Y-%m-%d %H:%M') AS end_time,
            text,
            geojson,
            scenario,
            schedule,
            timesheets_json,
            associated_aip_gml_id,
            lower_limit_ft,
            upper_limit_ft,
            effective_start,
            effective_end
        FROM notams
        WHERE status = 'ACTIVE'
    """
    params = []

    if sel_start_utc is not None and sel_end_utc is not None:
        query += " AND effective_start IS NOT NULL AND effective_end IS NOT NULL"
        query += " AND effective_start <= ? AND effective_end >= ?"
        params.extend([sel_end_utc, sel_start_utc])
    elif sel_start_utc is not None:
        query += " AND effective_end IS NOT NULL AND effective_end >= ?"
        params.append(sel_start_utc)
    elif sel_end_utc is not None:
        query += " AND effective_start IS NOT NULL AND effective_start <= ?"
        params.append(sel_end_utc)

    if alt_upper is not None:
        query += " AND (upper_limit_ft IS NULL OR upper_limit_ft >= ?)"
        params.append(alt_lower if alt_lower is not None else 0)
    if alt_lower is not None:
        query += " AND (lower_limit_ft IS NULL OR lower_limit_ft <= ?)"
        params.append(alt_upper if alt_upper is not None else 99999)

    cursor = con.cursor()
    try:
        rows = cursor.execute(query, params).fetchall()
    finally:
        cursor.close()

    features = []
    for r in rows:
        (notam_id, series, location, q_code, precision_type, operational_status,
         start_time, end_time, text, geojson_str, scenario, schedule,
         timesheets_json, associated_aip_gml_id, lower_limit_ft, upper_limit_ft,
         eff_start_raw, eff_end_raw) = r

        if sel_start_utc is not None and sel_end_utc is not None and eff_start_raw and eff_end_raw:
            if eff_start_raw.tzinfo is None:
                eff_start_utc = eff_start_raw.replace(tzinfo=UTC)
            else:
                eff_start_utc = eff_start_raw.astimezone(UTC)

            if eff_end_raw.tzinfo is None:
                eff_end_utc = eff_end_raw.replace(tzinfo=UTC)
            else:
                eff_end_utc = eff_end_raw.astimezone(UTC)

            if timesheets_json:
                timesheets = _parse_timesheets_json(timesheets_json)
                if not is_schedule_active_from_timesheets(timesheets, sel_start_utc, sel_end_utc):
                    continue
            elif schedule:
                if not is_schedule_active(schedule, eff_start_utc, eff_end_utc, sel_start_utc, sel_end_utc):
                    continue

        props = {
            "notam_id": notam_id,
            "series": series,
            "location": location,
            "q_code": q_code,
            "precision_type": precision_type,
            "operational_status": operational_status or "NORMAL",
            "start_time": start_time or "N/A",
            "end_time": end_time or "N/A",
            "text": text or "No Text",
            "scenario": scenario or "",
            "schedule": schedule or "",
            "associated_aip_gml_id": associated_aip_gml_id or "",
            "lower_limit_ft": lower_limit_ft,
            "upper_limit_ft": upper_limit_ft,
        }

        if associated_aip_gml_id:
            gml_ids = [g.strip() for g in associated_aip_gml_id.split(",") if g.strip()]
            aip_geoms = _fetch_aip_geojsons(request, gml_ids)
            if aip_geoms:
                for aip_geom in aip_geoms:
                    props_with_type = dict(props)
                    props_with_type["aip_feature_type"] = aip_geom["feature_type"]
                    features.append({"type": "Feature", "geometry": aip_geom["geometry"], "properties": props_with_type})
                continue

        if not geojson_str:
            continue
        try:
            geometry = json.loads(geojson_str)
            if geometry.get("type") == "GeometryCollection":
                for g in geometry.get("geometries", []):
                    features.append({"type": "Feature", "geometry": g, "properties": props})
            else:
                features.append({"type": "Feature", "geometry": geometry, "properties": props})
        except Exception:
            continue

    return JSONResponse(content={
        "type": "FeatureCollection",
        "features": features
    })


@app.get("/api/aip-features")
def get_aip_features(
    request: Request,
    target_time: str = Query(None, description="検索対象日時 (JST, YYYY-MM-DD HH:MM)"),
    feature_types: str = Query(None, description="カンマ区切りのフィーチャー種別 (例: AirportHeliport,Airspace)"),
    start_time: str = Query(None, description="互換用: NOTAMフィルター開始日時 (JST, YYYY-MM-DD HH:MM)"),
    end_time: str = Query(None, description="互換用: NOTAMフィルター終了日時 (JST, YYYY-MM-DD HH:MM)"),
    aip_date: str = Query(None, description="互換用: AIPデータ日付 (JST, YYYY-MM-DD)"),
):
    """
    DuckDBからAIPフィーチャーをGeoJSON形式で抽出 (仕様書 5.1 準拠)
    """
    con = getattr(request.app.state, "aip_con", None)
    if not con:
        return JSONResponse(content={"type": "FeatureCollection", "features": []})

    # target_time の優先設定 (未指定時は現在日時 UTC)
    time_str = target_time or start_time or aip_date
    if time_str:
        if len(time_str.strip()) == 10:  # YYYY-MM-DD
            time_str = f"{time_str.strip()} 00:00"
        target_time_utc = parse_jst_datetime(time_str)
    else:
        target_time_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    if target_time_utc is None:
        target_time_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    elif hasattr(target_time_utc, 'tzinfo') and target_time_utc.tzinfo is not None:
        target_time_utc = target_time_utc.astimezone(UTC).replace(tzinfo=None)

    sel_start_utc = parse_jst_datetime(start_time) if start_time else target_time_utc
    sel_end_utc = parse_jst_datetime(end_time) if end_time else None

    status_map = _build_notam_status_map(request, sel_start_utc, sel_end_utc)

    cursor = con.cursor()
    try:
        where_clauses = ["(f.valid_from <= ? OR f.valid_from IS NULL)", "(f.valid_to IS NULL OR f.valid_to > ?)"]
        params = [target_time_utc, target_time_utc]

        if feature_types:
            ft_list = [f.strip() for f in feature_types.split(",") if f.strip()]
            placeholders = ",".join(["?"] * len(ft_list))
            where_clauses.append(f"f.feature_type IN ({placeholders})")
            params.extend(ft_list)

        where_sql = " AND ".join(where_clauses)
        query = f"""
            SELECT DISTINCT
                f.gml_id, 
                f.feature_type, 
                f.name, 
                COALESCE(f.geojson, re.geojson) AS geojson, 
                f.parent_id, 
                f.properties_json,
                p.name AS parent_name
            FROM aip_features f
            LEFT JOIN aip_features p ON f.parent_id = p.gml_id AND p.name IS NOT NULL AND p.name != ''
            LEFT JOIN aip_features re ON f.feature_type = 'RunwayDirection' AND f.parent_id = re.parent_id AND re.feature_type = 'RunwayElement' AND re.geojson IS NOT NULL
            WHERE (f.geojson IS NOT NULL OR re.geojson IS NOT NULL) AND {where_sql}
        """

        rows = cursor.execute(query, params).fetchall()
    finally:
        cursor.close()

    type_fallback_map = {
        'TaxiwayElement': '誘導路 (Taxiway)',
        'Taxiway': '誘導路 (Taxiway)',
        'RunwayElement': '滑走路 (Runway)',
        'Runway': '滑走路 (Runway)',
        'RunwayDirection': '滑走路方向 (RunwayDirection)',
        'ApronElement': 'エプロン (Apron)',
        'Apron': 'エプロン (Apron)',
        'AircraftStand': '駐機位置 (AircraftStand)',
        'AirportHeliport': '空港 (Airport)',
        'Airspace': '空域 (Airspace)',
        'Navaid': '無線標識 (Navaid)',
        'DesignatedPoint': '通報地点 (Point)',
        'Route': '航空路 (Route)',
        'HoldingPattern': '待機パターン (HoldingPattern)'
    }

    features = []
    seen_gml_ids = set()  # gml_id重複排除用 (LEFT JOINによる複数行防止)
    for r in rows:
        gml_id, f_type, name, geojson_str, parent_id, properties_json, parent_name = r
        if not geojson_str:
            continue
        if gml_id in seen_gml_ids:
            continue
        seen_gml_ids.add(gml_id)
        try:
            geometry = json.loads(geojson_str)
            display_name = _extract_display_name(name or '')
            if not display_name and parent_name:
                display_name = _extract_display_name(parent_name or '')
            if not display_name:
                display_name = type_fallback_map.get(f_type, f_type)

            bearing = 0
            if f_type == 'RunwayDirection':
                # RunwayDirection個別の識別子 (例: 16L, 34R) を優先設定
                if name:
                    display_name = _extract_display_name(name)

                if geometry.get('type') == 'Polygon':
                    endpoint = _compute_runway_endpoint(geometry, display_name or '')
                    if endpoint:
                        geometry = endpoint

                import re
                m = re.match(r'(\d{2})', display_name or '')
                if m:
                    bearing = int(m.group(1)) * 10

            props = {
                "gml_id": gml_id,
                "feature_type": f_type,
                "name": display_name,
                "parent_id": parent_id,
                "bearing": bearing,
                "operational_status": status_map.get(gml_id, "NORMAL"),
            }
            if properties_json:
                try:
                    props["properties"] = json.loads(properties_json)
                except Exception:
                    pass

            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": props
            })
        except Exception:
            continue

    response = JSONResponse(content={
        "type": "FeatureCollection",
        "features": features
    })
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/api/notams/swim-search")
def search_swim_notams(
    location: str | None = Query(None, description="空港・地点略号 (複数時は+またはカンマ区切り、例: RJTT+RJAA)"),
    valid_start: str | None = Query(None, description="有効開始日時 (YYYY-MM-DD HH:MM または YYYYMMDDhhmm)"),
    valid_end: str | None = Query(None, description="有効終了日時 (YYYY-MM-DD HH:MM または YYYYMMDDhhmm)"),
    keyword: str | None = Query(None, description="本文検索キーワード"),
    series: str | None = Query(None, description="シリーズ記号 (例: A)"),
    notam_code: str | None = Query(None, description="ノータムコード (例: QMRLC)"),
    fir: str | None = Query(None, description="飛行情報区 (例: RJJJ)"),
):
    """国土交通省 SWIM WebAPI (デジタルノータムリクエストサービス) 経由でNOTAMを検索し、
    ICAO標準電文テキスト形式のリストを返却する。
    """
    try:
        results = swim_client.search_digital_notam(
            locations=location,
            valid_start=valid_start,
            valid_end=valid_end,
            keyword=keyword,
            series=series,
            notam_code=notam_code,
            fir=fir,
        )
        return JSONResponse(content={
            "status": "success",
            "count": len(results),
            "query": {
                "location": location,
                "valid_start": valid_start,
                "valid_end": valid_end,
                "keyword": keyword,
            },
            "notams": results,
        })
    except Exception as e:
        logger.error(f"Error in search_swim_notams endpoint: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e), "notams": []}
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
