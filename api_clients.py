import datetime
import math
import re
import urllib.parse
import pandas as pd
import requests
import streamlit as st
from pyproj import Transformer

from db_manager import save_snapshots_to_db


def normalize_service_key(raw_key: str) -> str:
    """공공데이터포털 인증키의 URL 인코딩을 정규화합니다."""
    return urllib.parse.unquote(raw_key.strip())


def ensure_list(item):
    """단일 dict 또는 None 응답을 항상 list로 변환합니다."""
    if item is None:
        return []
    if isinstance(item, list):
        return item
    return [item]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 위경도 좌표 간 거리(m)를 계산합니다."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@st.cache_resource
def get_tm_to_wgs84_transformer():
    """행정안전부 중부원점(Bessel TM) 좌표를 WGS84 위경도로 변환하는 변환기를 반환합니다."""
    bessel_tm = (
        "+proj=tmerc +lat_0=38 +lon_0=127.0028902777778 +k=1 "
        "+x_0=200000 +y_0=500000 +ellps=bessel +units=m +no_defs "
        "+towgs84=-115.80,474.99,674.11,1.16,-2.31,-1.63,6.43"
    )
    return Transformer.from_crs(bessel_tm, "EPSG:4326", always_xy=True)


def extract_nightly_price(price_item: dict):
    """SerpApi 가격 항목에서 1박 요금 숫자를 추출합니다."""
    for key in [
        "extracted_after_taxes_fees",
        "extracted_price",
        "extracted_display_price",
        "extracted_lowest",
    ]:
        val = price_item.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass

    for str_key in ["after_taxes_fees", "price", "display_price"]:
        raw = str(price_item.get(str_key) or "")
        digits = re.sub(r"[^\d.]", "", raw)
        if digits:
            try:
                return float(digits)
            except ValueError:
                pass
    return None


@st.cache_data(ttl=3600)
def geocode_kakao(address: str, kakao_key: str) -> dict:
    """카카오 로컬 API를 통해 주소의 좌표와 법정동/지번 코드를 추출합니다."""
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {kakao_key.strip()}"}
    resp = requests.get(url, headers=headers, params={"query": address}, timeout=10)
    resp.raise_for_status()
    docs = resp.json().get("documents", [])
    if not docs:
        raise ValueError("입력하신 주소를 카카오 로컬 API에서 찾을 수 없습니다.")

    doc = docs[0]
    addr_info = doc.get("address") or {}
    b_code = addr_info.get("b_code", "")
    if len(b_code) != 10:
        raise ValueError("법정동코드(b_code) 추출 실패: 지번 주소 형태로 다시 입력해 주세요.")

    return {
        "address_name": doc.get("address_name"),
        "road_address": (doc.get("road_address") or {}).get("address_name", ""),
        "lat": float(doc["y"]),
        "lng": float(doc["x"]),
        "sigunguCd": b_code[:5],
        "bjdongCd": b_code[5:],
        "platGbCd": "1" if addr_info.get("mountain_yn") == "Y" else "0",
        "bun": str(addr_info.get("main_address_no", "0")).zfill(4),
        "ji": str(addr_info.get("sub_address_no", "0")).zfill(4),
    }


@st.cache_data(ttl=3600)
def check_school_zone_kakao(lat: float, lng: float, kakao_key: str, radius: int = 200) -> list:
    """반경 200m 이내 교육환경보호구역 대상 학교(SC4)를 조회합니다."""
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    headers = {"Authorization": f"KakaoAK {kakao_key.strip()}"}
    params = {"category_group_code": "SC4", "x": lng, "y": lat, "radius": radius, "sort": "distance"}
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("documents", [])


@st.cache_data(ttl=3600)
def fetch_kakao_subway(lat: float, lng: float, kakao_key: str, radius: int = 1000) -> pd.DataFrame:
    """반경 내 지하철역(SW8) 목록과 거리를 조회합니다."""
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    headers = {"Authorization": f"KakaoAK {kakao_key.strip()}"}
    params = {
        "category_group_code": "SW8",
        "x": lng,
        "y": lat,
        "radius": radius,
        "size": 15,
        "sort": "distance",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    rows = []
    for d in resp.json().get("documents", []):
        dist_m = int(d.get("distance", 0))
        rows.append({
            "역명": d.get("place_name"),
            "거리(m)": dist_m,
            "예상도보(분)": max(1, round(dist_m / 67)),
            "lat": float(d["y"]),
            "lng": float(d["x"]),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=3600)
def fetch_kakao_lodging(lat: float, lng: float, kakao_key: str, radius: int = 1000) -> pd.DataFrame:
    """카카오 로컬 숙박업소(AD5) 목록을 조회합니다."""
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    headers = {"Authorization": f"KakaoAK {kakao_key.strip()}"}
    rows = []
    for page in range(1, 4):
        params = {
            "category_group_code": "AD5",
            "x": lng,
            "y": lat,
            "radius": radius,
            "page": page,
            "size": 15,
            "sort": "distance",
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for d in data.get("documents", []):
            rows.append({
                "업소명": d.get("place_name"),
                "세부분류": d.get("category_name", "").split(">")[-1].strip(),
                "주소": d.get("road_address_name") or d.get("address_name"),
                "거리(m)": int(d.get("distance", 0)),
                "lat": float(d["y"]),
                "lng": float(d["x"]),
                "URL": d.get("place_url"),
            })
        if data.get("meta", {}).get("is_end", True):
            break
    return pd.DataFrame(rows).drop_duplicates(subset=["업소명", "주소"]) if rows else pd.DataFrame()


@st.cache_data(ttl=3600)
def fetch_building_hub(geo: dict, data_key: str) -> dict:
    """국토교통부 건축물대장(표제부, 층별개요, 정화조, 지역지구)을 조회합니다."""
    base_url = "https://apis.data.go.kr/1613000/BldRgstHubService"
    key = normalize_service_key(data_key)
    common_params = {
        "serviceKey": key,
        "sigunguCd": geo["sigunguCd"],
        "bjdongCd": geo["bjdongCd"],
        "platGbCd": geo["platGbCd"],
        "bun": geo["bun"],
        "ji": geo["ji"],
        "numOfRows": 100,
        "pageNo": 1,
        "_type": "json",
    }
    endpoints = {
        "title": "getBrTitleInfo",
        "floors": "getBrFlrOulnInfo",
        "septic": "getBrWclfInfo",
        "zoning": "getBrJijiguInfo",
    }
    result = {}
    for name, ep in endpoints.items():
        last_err = None
        for attempt in range(3):
            try:
                r = requests.get(f"{base_url}/{ep}", params=common_params, timeout=20)
                r.raise_for_status()
                items = r.json().get("response", {}).get("body", {}).get("items", {}).get("item")
                result[name] = ensure_list(items)
                last_err = None
                break
            except Exception as e:
                last_err = str(e)
        if last_err is not None:
            result[name] = []
            result[f"{name}_error"] = last_err
    return result


@st.cache_data(ttl=3600)
def fetch_tour_api_poi(lat: float, lng: float, radius: int, data_key: str) -> pd.DataFrame:
    """한국관광공사 국문 관광정보 POI를 반경 기준으로 조회합니다."""
    url = "https://apis.data.go.kr/B551011/KorService2/locationBasedList2"
    key = normalize_service_key(data_key)
    type_map = {
        "12": "관광지",
        "14": "문화시설",
        "15": "축제/공연/행사",
        "28": "레포츠",
        "32": "숙박(공사등록)",
        "38": "쇼핑",
        "39": "음식점",
    }
    all_rows = []
    for page in range(1, 4):
        params = {
            "serviceKey": key,
            "MobileOS": "ETC",
            "MobileApp": "HostelConversionAnalyzer",
            "_type": "json",
            "arrange": "E",
            "mapX": lng,
            "mapY": lat,
            "radius": radius,
            "numOfRows": 100,
            "pageNo": page,
        }
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        body = r.json().get("response", {}).get("body", {})
        items = ensure_list((body.get("items") or {}).get("item"))
        if not items:
            break
        for it in items:
            ctype = str(it.get("contenttypeid", ""))
            all_rows.append({
                "명칭": it.get("title"),
                "카테고리": type_map.get(ctype, f"기타({ctype})"),
                "contentTypeId": ctype,
                "주소": it.get("addr1", ""),
                "거리(m)": round(float(it.get("dist", 0)), 1),
                "lat": float(it.get("mapy", 0)),
                "lng": float(it.get("mapx", 0)),
                "전화번호": it.get("tel", ""),
            })
        if len(items) < 100:
            break
    return pd.DataFrame(all_rows)


@st.cache_data(ttl=3600)
def fetch_mois_tourist_lodging(lat: float, lng: float, radius: int, data_key: str, max_pages: int = 10) -> pd.DataFrame:
    """행정안전부 관광숙박업 인허가 데이터를 조회해 반경 내 영업 중 업소를 필터링합니다."""
    url = "https://apis.data.go.kr/1741000/tourist_accommodations/info"
    key = normalize_service_key(data_key)
    transformer = get_tm_to_wgs84_transformer()
    matched = []
    for page in range(1, max_pages + 1):
        params = {"serviceKey": key, "pageNo": page, "numOfRows": 1000, "returnType": "json"}
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            payload = r.json()
            items = (
                payload.get("response", {}).get("body", {}).get("items", {}).get("item")
                or payload.get("body", {}).get("items")
                or []
            )
            items = ensure_list(items)
            if not items:
                break
            for it in items:
                row = {k.upper(): v for k, v in it.items()}
                status = str(
                    row.get("DTL_SALS_STTS_NM")
                    or row.get("SALS_STTS_NM")
                    or row.get("DTL_STAT_NM")
                    or row.get("TRD_STATE_NM")
                    or ""
                )
                if any(x in status for x in ["폐업", "취소", "말소", "휴업"]):
                    continue
                x_raw = row.get("X") or row.get("CRD_INFO_X")
                y_raw = row.get("Y") or row.get("CRD_INFO_Y")
                if not x_raw or not y_raw:
                    continue
                try:
                    t_lng, t_lat = transformer.transform(float(x_raw), float(y_raw))
                except Exception:
                    continue
                dist = haversine_m(lat, lng, t_lat, t_lng)
                if dist <= radius:
                    raw_rooms = row.get("GSRM_CNT")
                    rooms_val = int(float(raw_rooms)) if raw_rooms not in (None, "", "0") else None
                    raw_area = row.get("FCAR")
                    area_val = round(float(raw_area), 1) if raw_area not in (None, "", "0") else None
                    perm_date = str(row.get("LCPMT_YMD") or row.get("APV_PERM_YMD") or "").strip()
                    matched.append({
                        "사업장명": row.get("BPLC_NM", ""),
                        "업태구분": (
                            row.get("TOUR_LDGBIZ_DTL_NM")
                            or row.get("CUL_PHY_ED_US_NM")
                            or row.get("UPTAE_NM")
                            or "관광숙박업"
                        ),
                        "영업상태": status or "영업",
                        "영업시작일": perm_date,
                        "객실수(실)": rooms_val,
                        "시설면적(㎡)": area_val,
                        "총층수": row.get("TOTAL_NOFL") or row.get("GRND_NOFL") or "",
                        "도로명주소": (
                            row.get("ROAD_NM_ADDR")
                            or row.get("LOTNO_ADDR")
                            or row.get("RDN_WHL_ADDR")
                            or row.get("SITE_WHL_ADDR")
                            or ""
                        ),
                        "거리(m)": round(dist, 1),
                        "lat": t_lat,
                        "lng": t_lng,
                    })
            if len(items) < 1000:
                break
        except Exception:
            break
    df = pd.DataFrame(matched)
    return df.sort_values("거리(m)").reset_index(drop=True) if not df.empty else df


def fetch_google_hotel_summary(hotel_query: str, serpapi_key: str) -> dict:
    """SerpApi Google Hotels에서 숙소 토큰, 매칭명, 글로벌 OTA 평점/리뷰/성급/1박가를 조회합니다."""
    today = datetime.date.today()
    params = {
        "engine": "google_hotels",
        "q": hotel_query.strip(),
        "check_in_date": (today + datetime.timedelta(days=7)).strftime("%Y-%m-%d"),
        "check_out_date": (today + datetime.timedelta(days=8)).strftime("%Y-%m-%d"),
        "adults": 1,
        "currency": "KRW",
        "gl": "kr",
        "hl": "ko",
        "api_key": serpapi_key.strip(),
    }
    resp = requests.get("https://serpapi.com/search.json", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    target = data if data.get("property_token") else None
    if target is None:
        props = data.get("properties", [])
        if not props:
            raise ValueError(f"'{hotel_query}'에 해당하는 Google Hotels 등록 숙소를 찾지 못했습니다.")
        target = props[0]

    token = target.get("property_token")
    if not token:
        raise ValueError(f"'{hotel_query}'의 property_token을 추출할 수 없습니다.")

    rate_info = target.get("rate_per_night") or {}
    rate_val = extract_nightly_price(rate_info) if isinstance(rate_info, dict) else None

    return {
        "property_token": token,
        "resolved_title": target.get("name", hotel_query),
        "rating": target.get("overall_rating"),
        "reviews": target.get("reviews"),
        "hotel_class": target.get("extracted_hotel_class") or target.get("hotel_class") or "",
        "sample_rate": rate_val,
    }


def resolve_google_hotel_token(hotel_query: str, serpapi_key: str) -> tuple[str, str]:
    """SerpApi Google Hotels에서 숙소명으로부터 property_token과 매칭명을 조회합니다."""
    summary = fetch_google_hotel_summary(hotel_query, serpapi_key)
    return summary["property_token"], summary["resolved_title"]


def fetch_price_calendar_for_comp_set(
    comp_df: pd.DataFrame,
    serpapi_key: str,
    days_ahead: int = 21,
    adults: int = 1,
) -> pd.DataFrame:
    """등록된 Comp Set 숙소들의 향후 N일간 가격 캘린더를 수집하고 DB에 저장합니다."""
    today = datetime.date.today()
    captured_str = today.strftime("%Y-%m-%d")
    start_date = (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    end_date = (today + datetime.timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    records = []
    for _, row in comp_df.iterrows():
        hotel_label = row["구글매칭명"] or row["입력숙소명"]
        token = row["property_token"]

        params = {
            "engine": "google_hotels_price_calendar",
            "property_token": token,
            "start_date": start_date,
            "end_date": end_date,
            "adults": adults,
            "currency": "KRW",
            "gl": "kr",
            "hl": "ko",
            "api_key": serpapi_key.strip(),
        }
        r = requests.get("https://serpapi.com/search.json", params=params, timeout=15)
        r.raise_for_status()
        prices = r.json().get("prices", [])

        price_map = {}
        for p in prices:
            dt_str = p.get("date")
            val = extract_nightly_price(p)
            if dt_str and val is not None:
                price_map[dt_str] = val

        for i in range(1, days_ahead + 1):
            target_dt = today + datetime.timedelta(days=i)
            d_str = target_dt.strftime("%Y-%m-%d")
            day_type = "주말(금·토)" if target_dt.weekday() in (4, 5) else "평일(일~목)"
            rate = price_map.get(d_str)
            records.append({
                "수집일자": captured_str,
                "경쟁숙소": hotel_label,
                "기준인원": adults,
                "체크인일자": d_str,
                "요일구분": day_type,
                "노출판매가(원)": rate,
                "예약상태": "판매중" if rate is not None else "마감/미노출",
            })

    save_snapshots_to_db(records)
    return pd.DataFrame(records)
