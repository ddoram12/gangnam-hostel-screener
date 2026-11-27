import datetime
import os
from io import BytesIO

import folium
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium

from api_clients import (
    check_school_zone_kakao,
    discover_google_hotels_nearby,
    enrich_lodging_building_info,
    fetch_building_hub,
    fetch_google_hotel_summary,
    fetch_kakao_lodging,
    fetch_kakao_subway,
    fetch_mois_general_lodging,
    fetch_mois_tourist_lodging,
    fetch_tour_api_poi,
    geocode_kakao,
    resolve_google_hotel_token,
    fetch_price_calendar_for_comp_set,
)
from db_manager import (
    clear_comp_set_properties,
    delete_comp_set_property,
    get_saved_comp_set,
    init_db,
    load_historical_snapshots,
    upsert_comp_set_property,
)
from report_builder import (
    build_full_excel_bytes,
    build_printable_html_report,
    compute_per_hotel_summary,
)

load_dotenv()

st.set_page_config(
    page_title="서울 호스텔 시장조사 및 수지검토",
    page_icon="🏨",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2.5rem;
    }
    .hero-banner {
        background: linear-gradient(135deg, #0F172A 0%, #1E3A8A 60%, #2563EB 100%);
        padding: 1.5rem 2rem;
        border-radius: 14px;
        color: #FFFFFF;
        margin-bottom: 1.25rem;
        box-shadow: 0 4px 14px rgba(15, 23, 42, 0.12);
    }
    .hero-banner h1 {
        margin: 0;
        font-size: 1.65rem;
        font-weight: 700;
        letter-spacing: -0.02em;
        color: #FFFFFF;
    }
    .hero-banner p {
        margin: 0.45rem 0 0 0;
        font-size: 0.92rem;
        color: #DBEAFE;
    }
    div[data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 12px;
        padding: 0.9rem 1rem;
        box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
    }
    div[data-testid="stMetricLabel"] {
        font-size: 0.82rem;
        font-weight: 600;
        color: #475569;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.35rem;
        font-weight: 700;
        color: #0F172A;
    }
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
        border-bottom: 2px solid #E2E8F0;
        padding-bottom: 2px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px 8px 0 0;
        padding: 0.55rem 1rem;
        font-weight: 600;
        background-color: #F8FAFC;
        border: 1px solid #E2E8F0;
        border-bottom: none;
    }
    .stTabs [aria-selected="true"] {
        background-color: #EFF6FF !important;
        color: #1D4ED8 !important;
        border-color: #93C5FD !important;
    }
    .sidebar-brand {
        background: #F1F5F9;
        border-left: 4px solid #2563EB;
        padding: 0.75rem 0.9rem;
        border-radius: 0 8px 8px 0;
        margin-bottom: 1rem;
        font-size: 0.85rem;
        color: #334155;
    }
</style>
""", unsafe_allow_html=True)

init_db()


def get_secret(key_name: str) -> str:
    """로컬 .env 환경변수 또는 Streamlit Cloud Secrets에서 키를 안전하게 불러옵니다."""
    env_val = os.getenv(key_name, "")
    if env_val:
        return env_val
    try:
        return str(st.secrets.get(key_name, ""))
    except Exception:
        return ""


def evaluate_zoning_for_hostel(zoning_names: list[str]) -> tuple[str, str]:
    """등재된 용도지역을 바탕으로 호스텔(숙박시설) 용도변경 적합도를 판정합니다."""
    joined = " ".join(zoning_names)
    if any(k in joined for k in ["중심상업", "일반상업", "근린상업", "유통상업"]):
        return "success", "🟢 **상업지역 등재:** 호스텔(숙박시설) 용도변경에 가장 유리한 용도지역입니다. (단, 반경 200m 학교 및 지구단위계획 별도 확인 필요)"
    if "준주거" in joined:
        return "warning", "🟡 **준주거지역 등재:** 조건부 용도변경 검토가 필요합니다. (연접 도로 폭, 주거지 이격거리 및 관할 구청 지구단위계획 확인 필수)"
    if any(k in joined for k in ["일반주거", "전용주거"]):
        return "error", "🔴 **주거지역 등재 (주의):** 원칙적으로 일반 숙박시설 설치가 제한됩니다. 관광진흥법상 호스텔업 특례(도로 접도 요건 등) 적용 가능 여부를 지자체에 사전 확인해야 합니다."
    return "info", "📌 **용도지역 확인 필요:** 등재된 지역·지구 내 숙박시설 허용 여부를 건축사/구청 건축과에 사전 확인하세요."


# ---------------------------------------------------------
# 1. 시뮬레이터 세션 상태 초기화 (Tab 4 -> Tab 5 연동용)
# ---------------------------------------------------------
SIM_DEFAULTS = {
    "sim_dorm_wd_adr": 42000,
    "sim_dorm_we_adr": 58000,
    "sim_dorm_wd_occ": 70,
    "sim_dorm_we_occ": 88,
    "sim_priv_wd_adr": 95000,
    "sim_priv_we_adr": 135000,
    "sim_priv_wd_occ": 72,
    "sim_priv_we_occ": 90,
}
for k, v in SIM_DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ---------------------------------------------------------
# 2. API 키 내부 로드 및 사이드바(후보지·반경 설정)
# ---------------------------------------------------------
data_go_key = get_secret("DATA_GO_KR_KEY")
kakao_api_key = get_secret("KAKAO_REST_KEY")
serpapi_key = get_secret("SERPAPI_KEY")

st.sidebar.markdown("""
<div class="sidebar-brand">
    <b>🏨 서울 호스텔 시장조사 및 수지검토</b><br>
    서울 전역 임차 컨버전 입지·경쟁·수지 통합 분석
</div>
""", unsafe_allow_html=True)

st.sidebar.header("📍 후보 건물 및 분석 반경")
target_address = st.sidebar.text_input(
    "후보 건물 지번/도로명 주소",
    value="",
    placeholder="예: 서울특별시 중구 명동2가 54-2",
)
search_radius = st.sidebar.select_slider(
    "상권 분석 반경 (m)",
    options=[300, 500, 800, 1000, 1500],
    value=800,
)

run_btn = st.sidebar.button("🔍 입지 및 용도변경 스크리닝 실행", type="primary", width="stretch")

st.sidebar.markdown("---")
st.sidebar.markdown("**📄 전체 검토 내용 저장 · 인쇄**")
_sb_ar = st.session_state.get("analysis_result")
_sb_comp = get_saved_comp_set()
_sb_snap = st.session_state.get("latest_comp_snap")
_sb_sim = st.session_state.get("latest_sim_summary")
_sb_brk = st.session_state.get("latest_breakdown_df")
_today_tag = datetime.date.today().strftime("%Y%m%d")

st.sidebar.download_button(
    label="📥 전체 내용 저장 (Excel)",
    data=build_full_excel_bytes(_sb_ar, _sb_comp, _sb_snap, _sb_sim, _sb_brk),
    file_name=f"서울_호스텔_전체검토_{_today_tag}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    width="stretch",
    key="sidebar_download_excel",
)
st.sidebar.download_button(
    label="🖨️ 전체 내용 인쇄 / PDF",
    data=build_printable_html_report(_sb_ar, _sb_comp, _sb_snap, _sb_sim, _sb_brk).encode("utf-8"),
    file_name=f"서울_호스텔_인쇄보고서_{_today_tag}.html",
    mime="text/html",
    width="stretch",
    key="sidebar_download_print_html",
    help="내려받은 파일을 열면 바로 '인쇄 / PDF로 저장' 버튼으로 출력할 수 있습니다.",
)

# ---------------------------------------------------------
# 3. 메인 화면 실행 로직
# ---------------------------------------------------------
st.markdown("""
<div class="hero-banner">
    <h1>🏨 서울 호스텔 시장조사 및 수지검토</h1>
    <p>서울 전역 건축물대장 용도변경 적격성 + 역세권·관광 POI + Google Hotels Comp Set ADR·OCC + 마스터리스 Payback 시뮬레이터</p>
</div>
""", unsafe_allow_html=True)

if run_btn:
    if not target_address.strip():
        st.warning("좌측 입력란에 조사할 후보 건물의 지번 또는 도로명 주소를 입력해 주세요.")
        st.stop()
    if not data_go_key or not kakao_api_key:
        st.error("API 키가 설정되지 않았습니다. 로컬 `.env` 또는 클라우드 `Secrets` 설정을 확인해 주세요.")
        st.stop()

    with st.spinner("주소 지오코딩, 건축물대장, 지하철역, 관광공사 POI, 인허가 데이터를 수집 중입니다..."):
        try:
            geo = geocode_kakao(target_address, kakao_api_key)
            schools_200m = check_school_zone_kakao(geo["lat"], geo["lng"], kakao_api_key, radius=200)
            subway_df = fetch_kakao_subway(geo["lat"], geo["lng"], kakao_api_key, radius=search_radius)
            bld_data = fetch_building_hub(geo, data_go_key)
            tour_df = fetch_tour_api_poi(geo["lat"], geo["lng"], search_radius, data_go_key)
            kakao_lodging_df = fetch_kakao_lodging(geo["lat"], geo["lng"], kakao_api_key, radius=search_radius)
            kakao_lodging_df = enrich_lodging_building_info(kakao_lodging_df, kakao_api_key, data_go_key, limit=15)

            gu_candidates = []
            for addr_str in [geo.get("address_name", "")] + (kakao_lodging_df["주소"].dropna().tolist() if not kakao_lodging_df.empty else []):
                for tok in str(addr_str).split():
                    if tok.endswith("구") and tok not in gu_candidates:
                        gu_candidates.append(tok)
            if not gu_candidates:
                gu_candidates = [""]

            mois_t_list = [fetch_mois_tourist_lodging(geo["lat"], geo["lng"], search_radius, data_go_key, sigungu_name=g) for g in gu_candidates[:3]]
            mois_g_list = [fetch_mois_general_lodging(geo["lat"], geo["lng"], search_radius, data_go_key, sigungu_name=g) for g in gu_candidates[:3]]
            mois_df = pd.concat([d for d in mois_t_list if not d.empty], ignore_index=True) if any(not d.empty for d in mois_t_list) else pd.DataFrame()
            mois_gen_df = pd.concat([d for d in mois_g_list if not d.empty], ignore_index=True) if any(not d.empty for d in mois_g_list) else pd.DataFrame()

            st.session_state["analysis_result"] = {
                "geo": geo,
                "schools_200m": schools_200m,
                "subway_df": subway_df,
                "bld_data": bld_data,
                "tour_df": tour_df,
                "mois_df": mois_df,
                "mois_gen_df": mois_gen_df,
                "kakao_lodging_df": kakao_lodging_df,
                "radius": search_radius,
            }
        except Exception as e:
            st.error(f"데이터 조회 중 오류가 발생했습니다: {e}")
            st.stop()

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "1. 용도변경 인허가·물리적 진단",
    "2. 상권 집객 & 역세권·경쟁 숙박",
    "3. 입지 지도 시각화",
    "4. 직접 경쟁군(Comp Set) ADR·OCC 추적",
    "5. 임차(마스터리스) 정밀 수지 시뮬레이터",
])

if "analysis_result" not in st.session_state:
    for t in (tab1, tab2, tab3):
        with t:
            st.info("좌측 사이드바에서 후보 건물 주소를 입력하고 **[입지 및 용도변경 스크리닝 실행]**을 눌러주세요.")
else:
    res = st.session_state["analysis_result"]
    geo = res["geo"]
    schools_200m = res["schools_200m"]
    subway_df = res.get("subway_df", pd.DataFrame())
    bld_data = res["bld_data"]
    tour_df = res["tour_df"]
    mois_df = res["mois_df"]
    mois_gen_df = res.get("mois_gen_df", pd.DataFrame())
    kakao_lodging_df = res["kakao_lodging_df"]
    radius = res["radius"]

    titles = bld_data.get("title", [])
    main_title = titles[0] if titles else {}
    bld_name = main_title.get("bldNm") or "건물명 미등재"
    main_purps = main_title.get("mainPurpsCdNm") or "정보없음"
    tot_area = float(main_title.get("totArea") or 0)
    grnd_flr = main_title.get("grndFlrCnt") or "-"
    ugrnd_flr = main_title.get("ugrndFlrCnt") or "-"
    ride_elvt = int(main_title.get("rideUseElvtCnt") or 0)
    vltn_yn = str(main_title.get("vltnBldYn") or "0")

    septic_list = bld_data.get("septic", [])
    septic_capa = sum(int(float(s.get("capaPsper") or 0)) for s in septic_list)
    zoning_list = bld_data.get("zoning", [])
    zoning_names = list({z.get("jijiguCdNm") for z in zoning_list if z.get("jijiguCdNm")})
    poi_non_lodging = tour_df[tour_df["contentTypeId"] != "32"] if not tour_df.empty else pd.DataFrame()

    # [TAB 1] 건축물대장 & 법적 리스크 진단
    with tab1:
        st.subheader(f"대상 건축물 개요: {bld_name} ({geo['address_name']})")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("현재 주용도", main_purps)
        c2.metric("연면적", f"{tot_area:,.1f} ㎡ ({tot_area * 0.3025:,.1f} 평)")
        c3.metric("규모 / 승강기", f"지하{ugrnd_flr}/지상{grnd_flr}층 · EV {ride_elvt}대")
        c4.metric("정화조 처리용량", f"{septic_capa:,} 인" if septic_capa > 0 else "하수직결/미등재")
        c5.metric("반경 200m 내 학교", f"{len(schools_200m)} 개소")

        st.markdown("#### 임차 용도변경 사전 Red Flag & 용도지역 적합도 체크")
        if vltn_yn == "1":
            st.error("🚨 **[치명적 리스크] 위반건축물 등재 건물입니다.** 원상복구 전까지 숙박시설 용도변경 인허가가 불허됩니다.")
        else:
            st.success("✅ **위반건축물 미등재:** 건축물대장 표제부상 위반건축물 표기가 없습니다.")

        if len(schools_200m) > 0:
            school_names = ", ".join([f"{s['place_name']}({s['distance']}m)" for s in schools_200m])
            st.warning(f"⚠️ **[교육환경보호구역 주의] 반경 200m 이내 학교 존재:** {school_names}")
        else:
            st.success("✅ **교육환경보호구역(상대보호구역 200m) 해당 학교 없음**")

        if zoning_names:
            st.info(f"📌 **등재 지역·지구·구역:** {', '.join(zoning_names)}")
            level, msg = evaluate_zoning_for_hostel(zoning_names)
            getattr(st, level)(msg)

        if septic_capa > 0 and tot_area > 0:
            est_max_beds_by_area = int((tot_area * 0.3025) / 2.5)
            if septic_capa < est_max_beds_by_area:
                st.warning(
                    f"⚠️ **정화조 용량 점검 필요:** 면적 기준 최대 도미토리 추정 수용({est_max_beds_by_area}베드) 대비 "
                    f"정화조 처리용량({septic_capa}인)이 부족할 수 있어 증설/하수직결 여부를 확인해야 합니다."
                )

        col_f, col_s = st.columns([2, 1])
        with col_f:
            st.markdown("#### 층별 개요")
            floors = bld_data.get("floors", [])
            if floors:
                df_flr = pd.DataFrame([{
                    "층구분": f.get("flrGbCdNm", ""),
                    "층수": f.get("flrNoNm", ""),
                    "주용도": f.get("mainPurpsCdNm", ""),
                    "기타용도": f.get("etcPurps", ""),
                    "면적(㎡)": float(f.get("area") or 0),
                    "면적(평)": round(float(f.get("area") or 0) * 0.3025, 2),
                } for f in floors])
                st.dataframe(df_flr, width="stretch", hide_index=True)
        with col_s:
            st.markdown("#### 오수정화시설 상세")
            if septic_list:
                df_sep = pd.DataFrame([{
                    "형식": s.get("modeCdNm", ""),
                    "처리대상인원": s.get("capaPsper", ""),
                    "용량(㎥)": s.get("capaLube", ""),
                } for s in septic_list])
                st.dataframe(df_sep, width="stretch", hide_index=True)
            else:
                st.write("오수정화시설 등재 내역 없음")

    # [TAB 2] 관광공사 집객 POI & 역세권 & 경쟁 숙박 공급량
    with tab2:
        st.subheader(f"반경 {radius}m 이내 역세권·집객 동인 및 경쟁 숙박 공급 분석")
        nearest_subway = (
            f"{subway_df.iloc[0]['역명']} ({subway_df.iloc[0]['거리(m)']}m / 도보 {subway_df.iloc[0]['예상도보(분)']}분)"
            if not subway_df.empty else "반경 내 없음"
        )
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("최인접 지하철역", nearest_subway)
        m2.metric("반경 내 지하철역", f"{len(subway_df)} 개역")
        m3.metric("관광공사 집객 POI", f"{len(poi_non_lodging)} 건")
        m4.metric("행안부 인허가 관광숙박", f"{len(mois_df)} 건")
        m5.metric("카카오 로컬 전체 숙박", f"{len(kakao_lodging_df)} 건")

        if not subway_df.empty:
            with st.expander("🚇 반경 내 지하철역 접근성 상세", expanded=False):
                st.dataframe(subway_df[["역명", "거리(m)", "예상도보(분)"]], width="stretch", hide_index=True)

        col_t1, col_t2 = st.columns(2)
        with col_t1:
            st.markdown("#### 한국관광공사 집객 POI 분포")
            if not poi_non_lodging.empty:
                cat_counts = poi_non_lodging["카테고리"].value_counts()
                st.bar_chart(cat_counts, height=180)
                st.dataframe(poi_non_lodging[["명칭", "카테고리", "거리(m)", "주소"]], width="stretch", hide_index=True, height=280)
        with col_t2:
            st.markdown("#### 반경 내 숙박업소 목록 (Comp Set 후보 발굴용)")
            st.caption("💡 아래 표에서 원하는 숙소 행(또는 왼쪽 체크박스)을 클릭하면 하단 Comp Set 후보 선택창에 자동으로 담깁니다.")
            sub_b, sub_a = st.tabs(["카카오 로컬 전체 숙박(AD5)", "행안부 인허가 전체 (관광+일반숙박)"])

            def _norm_addr(raw: str) -> str:
                s = str(raw or "").split(",")[0].split("(")[0].strip()
                for full, short in [("서울특별시", "서울"), ("부산광역시", "부산"), ("대구광역시", "대구"), ("인천광역시", "인천"), ("광주광역시", "광주"), ("대전광역시", "대전"), ("울산광역시", "울산")]:
                    s = s.replace(full, short)
                return s.replace(" ", "").lower()

            mois_all_df = pd.concat([d for d in (mois_df, mois_gen_df) if not d.empty], ignore_index=True) if (not mois_df.empty or not mois_gen_df.empty) else pd.DataFrame()
            if not mois_all_df.empty:
                mois_all_df["_dedup_key"] = mois_all_df["사업장명"].astype(str).str.replace(" ", "", regex=False).str.lower()
                mois_all_df["_has_area"] = mois_all_df["시설면적(㎡)"].notna() if "시설면적(㎡)" in mois_all_df.columns else False
                mois_all_df = (
                    mois_all_df.sort_values(["거리(m)", "_has_area"], ascending=[True, False])
                    .drop_duplicates(subset=["_dedup_key"], keep="first")
                    .drop(columns=["_dedup_key", "_has_area"], errors="ignore")
                    .reset_index(drop=True)
                )
            mois_meta_map = {}
            mois_addr_map = {}
            mois_records = []
            if not mois_all_df.empty:
                for _, mr in mois_all_df.iterrows():
                    meta = {
                        "영업시작일": mr.get("영업시작일", ""),
                        "객실수(실)": mr.get("객실수(실)"),
                        "lat": mr.get("lat"),
                        "lng": mr.get("lng"),
                    }
                    mois_records.append(meta)
                    norm_n = str(mr["사업장명"]).replace(" ", "").lower()
                    if norm_n:
                        mois_meta_map[norm_n] = meta
                    raw_addr = _norm_addr(mr.get("도로명주소", ""))
                    if raw_addr:
                        mois_addr_map[raw_addr] = meta

            table_picked = []
            with sub_b:
                if not kakao_lodging_df.empty:
                    from api_clients import haversine_m
                    k_disp = kakao_lodging_df.copy().reset_index(drop=True)
                    matched_dates, matched_rooms = [], []
                    for _, kr in k_disp.iterrows():
                        kn = str(kr["업소명"]).replace(" ", "").lower()
                        ka = _norm_addr(kr["주소"])
                        found = mois_meta_map.get(kn)
                        if not found:
                            found = next((v for mk, v in mois_meta_map.items() if mk and (mk in kn or kn in mk)), None)
                        if not found and ka:
                            found = mois_addr_map.get(ka) or next((v for ma, v in mois_addr_map.items() if ka in ma or ma in ka), None)
                        if not found and mois_records:
                            klat, klng = float(kr["lat"]), float(kr["lng"])
                            closest = min(
                                (m for m in mois_records if m.get("lat") and m.get("lng")),
                                key=lambda m: haversine_m(klat, klng, float(m["lat"]), float(m["lng"])),
                                default=None,
                            )
                            if closest and haversine_m(klat, klng, float(closest["lat"]), float(closest["lng"])) <= 35:
                                found = closest
                        matched_dates.append((found["영업시작일"] or "-") if found else "-")
                        r_cnt = found.get("객실수(실)") if found else None
                        matched_rooms.append(f"{int(r_cnt)}실" if pd.notna(r_cnt) and r_cnt else "-")
                    k_disp["영업시작일(인허가)"] = matched_dates
                    k_disp["객실수"] = matched_rooms
                    disp_cols = [
                        c for c in ["업소명", "세부분류", "거리(m)", "영업시작일(인허가)", "객실수", "준공년월", "건물규모", "주소"]
                        if c in k_disp.columns
                    ]
                    k_event = st.dataframe(
                        k_disp[disp_cols],
                        width="stretch",
                        hide_index=True,
                        height=300,
                        on_select="rerun",
                        selection_mode="multi-row",
                        key="kakao_lodging_table",
                    )
                    for row_idx in k_event.selection.rows:
                        if row_idx < len(k_disp):
                            table_picked.append(str(k_disp.loc[row_idx, "업소명"]).strip())
                else:
                    st.info("반경 내 카카오 로컬 숙박업소 조회 결과가 없습니다.")

            with sub_a:
                if not mois_all_df.empty:
                    mois_all_sorted = mois_all_df.sort_values("거리(m)").reset_index(drop=True)
                    mois_cols = [c for c in ["사업장명", "업태구분", "영업시작일", "객실수(실)", "시설면적(㎡)", "총층수", "거리(m)", "도로명주소"] if c in mois_all_sorted.columns]
                    m_event = st.dataframe(
                        mois_all_sorted[mois_cols],
                        width="stretch",
                        hide_index=True,
                        height=300,
                        on_select="rerun",
                        selection_mode="multi-row",
                        key="mois_lodging_table",
                    )
                    for row_idx in m_event.selection.rows:
                        if row_idx < len(mois_all_sorted):
                            table_picked.append(str(mois_all_sorted.loc[row_idx, "사업장명"]).strip())
                else:
                    st.info("반경 내 행안부 인허가 숙박업소 조회 결과가 없습니다.")

            # Comp Set 후보 선택 -> OTA 평점 조회 & 4번 탭 바로 등록
            st.markdown("##### 🎯 Comp Set 후보 OTA 평점 확인 및 4번 탭 바로 등록")
            cand_list = []
            if not mois_all_df.empty:
                cand_list.extend(mois_all_df["사업장명"].dropna().tolist())
            if not kakao_lodging_df.empty:
                cand_list.extend(kakao_lodging_df["업소명"].dropna().tolist())
            cand_options = list(dict.fromkeys([c.strip() for c in cand_list if str(c).strip()]))

            table_picked_unique = list(dict.fromkeys([p for p in table_picked if p in cand_options]))
            if st.session_state.get("_prev_table_picked") != table_picked_unique:
                st.session_state["_prev_table_picked"] = table_picked_unique
                st.session_state["tab2_comp_candidates"] = table_picked_unique

            if cand_options:
                selected_cands = st.multiselect(
                    "비교/등록할 숙소를 선택하세요 (1~5곳 권장)",
                    options=cand_options,
                    key="tab2_comp_candidates",
                )
                btn_c1, btn_c2 = st.columns(2)
                check_ota_btn = btn_c1.button("🌐 선택 숙소 글로벌 OTA 평점 조회", width="stretch", key="btn_check_ota")
                reg_comp_btn = btn_c2.button("➕ 4번 Comp Set에 바로 등록", type="primary", width="stretch", key="btn_reg_comp")

                if check_ota_btn:
                    if not selected_cands:
                        st.warning("조회할 숙소를 먼저 선택해 주세요.")
                    elif not serpapi_key:
                        st.error("SerpApi 키가 설정되지 않았습니다. 로컬 `.env` 또는 클라우드 `Secrets` 설정을 확인해 주세요.")
                    else:
                        ota_rows = []
                        ota_cache = st.session_state.setdefault("ota_summary_cache", {})
                        r_tokens = [tok for tok in str(geo.get("address_name", "")).split() if tok.endswith(("구", "시"))]
                        r_hint = next((t for t in r_tokens if t.endswith("구")), "서울")
                        with st.spinner("Google Hotels에서 글로벌 OTA 평점·리뷰·참고가를 조회 중입니다..."):
                            for name in selected_cands:
                                try:
                                    info = fetch_google_hotel_summary(name, serpapi_key, region_hint=r_hint)
                                    ota_cache[name] = info
                                    ota_rows.append({
                                        "선택숙소": name,
                                        "구글매칭명": info["resolved_title"],
                                        "OTA평점": f"⭐ {info['rating']}" if info["rating"] else "-",
                                        "리뷰수": f"{int(info['reviews']):,}" if info["reviews"] else "-",
                                        "성급": f"{info['hotel_class']}성" if info["hotel_class"] else "-",
                                        "참고1박가(원)": f"{int(info['sample_rate']):,}" if info["sample_rate"] else "-",
                                    })
                                except Exception as e:
                                    ota_rows.append({
                                        "선택숙소": name,
                                        "구글매칭명": f"조회실패({e})",
                                        "OTA평점": "-",
                                        "리뷰수": "-",
                                        "성급": "-",
                                        "참고1박가(원)": "-",
                                    })
                        st.session_state["tab2_ota_preview"] = pd.DataFrame(ota_rows)

                if "tab2_ota_preview" in st.session_state and not st.session_state["tab2_ota_preview"].empty:
                    st.dataframe(st.session_state["tab2_ota_preview"], width="stretch", hide_index=True)

                if reg_comp_btn:
                    if not selected_cands:
                        st.warning("등록할 숙소를 먼저 선택해 주세요.")
                    elif not serpapi_key:
                        st.error("SerpApi 키가 설정되지 않았습니다. 로컬 `.env` 또는 클라우드 `Secrets` 설정을 확인해 주세요.")
                    else:
                        ota_cache = st.session_state.setdefault("ota_summary_cache", {})
                        r_tokens = [tok for tok in str(geo.get("address_name", "")).split() if tok.endswith(("구", "시"))]
                        r_hint = next((t for t in r_tokens if t.endswith("구")), "서울")
                        ok_cnt, fail_msgs = 0, []
                        with st.spinner("선택한 숙소를 4번 Comp Set에 등록 중입니다..."):
                            for name in selected_cands:
                                try:
                                    info = ota_cache.get(name) or fetch_google_hotel_summary(name, serpapi_key, region_hint=r_hint)
                                    ota_cache[name] = info
                                    note_parts = []
                                    norm_n = name.replace(" ", "").lower()
                                    m_meta = mois_meta_map.get(norm_n)
                                    if m_meta:
                                        if m_meta.get("영업시작일"):
                                            note_parts.append(f"개업:{m_meta['영업시작일']}")
                                        if m_meta.get("객실수(실)"):
                                            note_parts.append(f"{m_meta['객실수(실)']}실")
                                    if info.get("rating"):
                                        note_parts.append(f"평점:{info['rating']}")
                                    if info.get("reviews"):
                                        note_parts.append(f"리뷰:{int(info['reviews']):,}")
                                    upsert_comp_set_property(
                                        name,
                                        info["property_token"],
                                        info["resolved_title"],
                                        " / ".join(note_parts),
                                    )
                                    ok_cnt += 1
                                except Exception as e:
                                    fail_msgs.append(f"{name}: {e}")
                        if ok_cnt > 0:
                            st.success(f"✅ {ok_cnt}개 숙소가 4번 탭 [직접 경쟁군(Comp Set)]에 등록되었습니다! 상단 4번 탭에서 바로 가격 캘린더를 수집해 보세요.")
                        for fm in fail_msgs:
                            st.warning(f"⚠️ 등록 실패 - {fm}")
                        if fail_msgs:
                            st.info("💡 소형/신규 호스텔은 Google Hotels에 미등록되어 있을 수 있습니다. **4번 탭 상단의 [주변 Google Hotels 등록 숙소 자동 찾기]**를 이용하시면 가격 추적이 가능한 인근 경쟁 숙소를 바로 찾으실 수 있습니다!")

    # [TAB 3] 지도 시각화
    with tab3:
        st.subheader("후보 부지 중심 지하철역·학교보호구역·관광 POI·경쟁 숙박 분포 지도")
        st.caption("🔴 후보지 | 🟢 지하철역 | 🟠 반경 200m 학교 | 🔵 관광 POI | 🟣 행안부 관광숙박 | 🟡 카카오 숙박")

        def make_popup(badge: str, title: str, sub: str = "", badge_color: str = "#2563EB") -> folium.Popup:
            sub_html = f'<div style="font-size:12px;color:#64748B;margin-top:3px;">{sub}</div>' if sub else ""
            html = (
                f'<div style="min-width:180px;max-width:260px;font-family:sans-serif;line-height:1.4;">'
                f'<span style="display:inline-block;background:{badge_color};color:#fff;font-size:11px;'
                f'font-weight:700;padding:2px 7px;border-radius:10px;margin-bottom:4px;">{badge}</span>'
                f'<div style="font-size:13px;font-weight:700;color:#0F172A;word-break:keep-all;">{title}</div>'
                f'{sub_html}</div>'
            )
            return folium.Popup(html, max_width=280)

        m = folium.Map(location=[geo["lat"], geo["lng"]], zoom_start=15, tiles="OpenStreetMap")
        folium.Circle(location=[geo["lat"], geo["lng"]], radius=radius, color="#2563EB", fill=True, fill_opacity=0.06).add_to(m)
        folium.Circle(
            location=[geo["lat"], geo["lng"]],
            radius=200,
            color="#DC2626",
            dash_array="5,5",
            fill=False,
            popup=make_popup("법규체크", "학교 환경보호구역 체크 반경", "반경 200m", "#DC2626"),
        ).add_to(m)
        folium.Marker(
            [geo["lat"], geo["lng"]],
            popup=make_popup("후보지", bld_name, geo["address_name"], "#DC2626"),
            tooltip=f"[후보지] {bld_name}",
            icon=folium.Icon(color="red", icon="star"),
        ).add_to(m)

        if not subway_df.empty:
            for _, r in subway_df.iterrows():
                folium.Marker(
                    [r["lat"], r["lng"]],
                    popup=make_popup("지하철", r["역명"], f"거리 {r['거리(m)']}m · 도보 약 {r['예상도보(분)']}분", "#16A34A"),
                    tooltip=f"[지하철] {r['역명']} ({r['거리(m)']}m)",
                    icon=folium.Icon(color="green", icon="train", prefix="fa"),
                ).add_to(m)

        for s in schools_200m:
            folium.Marker(
                [float(s["y"]), float(s["x"])],
                popup=make_popup("학교(200m내)", s["place_name"], f"거리 {s['distance']}m", "#EA580C"),
                tooltip=f"[학교] {s['place_name']} ({s['distance']}m)",
                icon=folium.Icon(color="orange", icon="exclamation-sign"),
            ).add_to(m)

        if not poi_non_lodging.empty:
            for _, r in poi_non_lodging.head(80).iterrows():
                folium.CircleMarker(
                    [r["lat"], r["lng"]],
                    radius=5,
                    color="#0284C7",
                    fill=True,
                    popup=make_popup(r["카테고리"], r["명칭"], f"거리 {r['거리(m)']}m", "#0284C7"),
                    tooltip=f"[{r['카테고리']}] {r['명칭']}",
                ).add_to(m)
        if not mois_df.empty:
            for _, r in mois_df.iterrows():
                folium.Marker(
                    [r["lat"], r["lng"]],
                    popup=make_popup("관광숙박업", r["사업장명"], f"{r['업태구분']} · {r['거리(m)']}m", "#9333EA"),
                    tooltip=f"[관광숙박] {r['사업장명']}",
                    icon=folium.Icon(color="purple", icon="bed", prefix="fa"),
                ).add_to(m)
        if not kakao_lodging_df.empty:
            for _, r in kakao_lodging_df.iterrows():
                folium.CircleMarker(
                    [r["lat"], r["lng"]],
                    radius=6,
                    color="#EA580C",
                    fill=True,
                    popup=make_popup("카카오 숙박", r["업소명"], f"{r['세부분류']} · {r['거리(m)']}m", "#EA580C"),
                    tooltip=f"[카카오] {r['업소명']} ({r['거리(m)']}m)",
                ).add_to(m)
        st_folium(m, width=1200, height=540, returned_objects=[])

# ---------------------------------------------------------
# [TAB 4] 직접 경쟁군(Comp Set) 3~5곳 핀포인트 ADR·OCC 추적
# ---------------------------------------------------------
with tab4:
    st.subheader("직접 경쟁군(Comp Set) 3~5곳 핀포인트 단가·가동률 추적")
    st.caption("1인 기준(도미토리)과 2인 기준(프라이빗룸)을 각각 수집해 5번 시뮬레이터의 평일·주말 변수로 분리 주입할 수 있습니다.")

    with st.expander("🔍 주변 Google Hotels 등록 숙소 자동 찾기 (미등록 숙소 대안)", expanded=False):
        st.caption("2번 탭의 소형 숙소가 Google Hotels에 미등록된 경우, 해당 지역에서 실제 가격·가동률 추적이 가능한 인근 호스텔/호텔을 한 번에 찾아 등록할 수 있습니다.")
        default_region_q = "서울 강남구"
        if "analysis_result" in st.session_state:
            addr_parts = str(st.session_state["analysis_result"]["geo"].get("address_name", "")).split()
            if len(addr_parts) >= 2:
                default_region_q = " ".join(addr_parts[:3])
        d_c1, d_c2 = st.columns([2.5, 1.5])
        region_q = d_c1.text_input("검색 지역/상권 (예: 서울 광진구 화양동, 건대입구, 명동)", value=default_region_q, key="discover_region_q")
        with d_c2:
            st.markdown("<br>", unsafe_allow_html=True)
            discover_btn = st.button("🌐 추적 가능 숙소 찾기 (API 1회)", width="stretch", key="btn_discover_gh")
        if discover_btn:
            if not serpapi_key:
                st.error("SerpApi 키가 설정되지 않았습니다. 로컬 `.env` 또는 클라우드 `Secrets` 설정을 확인해 주세요.")
            elif not region_q.strip():
                st.warning("검색할 지역이나 상권명을 입력해 주세요.")
            else:
                try:
                    with st.spinner("Google Hotels에서 해당 지역 등록 숙소를 조회 중입니다..."):
                        st.session_state["discovered_gh_list"] = discover_google_hotels_nearby(region_q, serpapi_key)
                except Exception as e:
                    st.error(f"조회 실패: {e}")

        if st.session_state.get("discovered_gh_list"):
            disc_list = st.session_state["discovered_gh_list"]
            exclude_rentals = st.checkbox("1실 독채/공유숙박 제외 (호텔·호스텔만 보기)", value=True, key="chk_exclude_rentals")

            # 2번 탭 행안부 인허가 데이터와 매칭해 실제 객실수(실) 보강
            room_lookup = {}
            if "analysis_result" in st.session_state:
                ar_disc = st.session_state["analysis_result"]
                for src_d in (ar_disc.get("mois_df", pd.DataFrame()), ar_disc.get("mois_gen_df", pd.DataFrame())):
                    if not src_d.empty:
                        for _, mr in src_d.iterrows():
                            r_cnt = mr.get("객실수(실)")
                            if pd.notna(r_cnt) and r_cnt:
                                rk = str(mr["사업장명"]).replace(" ", "").lower()
                                room_lookup[rk] = int(r_cnt)

            enriched_disc = []
            for d in disc_list:
                is_r = d.get("is_rental", False)
                if exclude_rentals and is_r:
                    continue
                dk = str(d["숙소명"]).replace(" ", "").lower()
                matched_r = room_lookup.get(dk) or next((v for k, v in room_lookup.items() if k and (k in dk or dk in k)), None)
                if matched_r:
                    room_str = f"{matched_r}실"
                elif is_r:
                    room_str = "1실(독채)"
                else:
                    room_str = "다객실(미확인)"
                enriched_disc.append({
                    "숙소명": d["숙소명"],
                    "숙소유형": d.get("숙소유형", "호텔/호스텔"),
                    "객실수": room_str,
                    "OTA평점": d.get("OTA평점"),
                    "리뷰수": d.get("리뷰수"),
                    "참고1박가(원)": d.get("참고1박가(원)"),
                })

            if not enriched_disc:
                st.info("표시할 호텔/호스텔이 없습니다. 위 체크박스를 해제하면 공유숙박도 함께 볼 수 있습니다.")
            else:
                disc_df = pd.DataFrame(enriched_disc)
                st.caption("💡 아래 표에서 원하는 숙소 행(또는 왼쪽 체크박스)을 클릭하면 하단 선택창에 자동으로 담깁니다.")
                disc_event = st.dataframe(
                    disc_df,
                    width="stretch",
                    hide_index=True,
                    on_select="rerun",
                    selection_mode="multi-row",
                    key="disc_gh_table",
                )
                disc_table_picked = [
                    str(disc_df.iloc[idx]["숙소명"])
                    for idx in disc_event.selection.rows
                    if idx < len(disc_df)
                ]
                if st.session_state.get("_prev_disc_picked") != disc_table_picked:
                    st.session_state["_prev_disc_picked"] = disc_table_picked
                    st.session_state["picked_disc_hotels"] = disc_table_picked

                picked_disc = st.multiselect("Comp Set으로 등록할 숙소 선택", options=[d["숙소명"] for d in enriched_disc], key="picked_disc_hotels")
                if st.button("➕ 선택한 숙소 즉시 Comp Set 등록 (추가 API 소모 없음)", type="primary", key="btn_reg_disc"):
                    if not picked_disc:
                        st.warning("등록할 숙소를 먼저 선택해 주세요.")
                    else:
                        for d_item in disc_list:
                            if d_item["숙소명"] in picked_disc:
                                note_s = f"{d_item.get('숙소유형', '')} / 평점:{d_item['OTA평점'] or '-'} / 리뷰:{d_item['리뷰수'] or '-'}"
                                upsert_comp_set_property(d_item["숙소명"], d_item["property_token"], d_item["숙소명"], note_s)
                        st.success(f"✅ {len(picked_disc)}개 숙소가 등록되었습니다!")
                        st.rerun()

    with st.expander("➕ Comp Set 경쟁 호스텔 추가 / 삭제 관리", expanded=True):
        st.caption("💡 서울권 예시 검색어: `위코스테이 명동`, `오클라우드 호텔 강남`, `신라스테이 역삼`, `나인트리 명동` 등 Google Hotels에 등록된 실제 상호를 입력하세요.")
        c_add1, c_add2, c_add3 = st.columns([2, 2, 1])
        new_hotel_name = c_add1.text_input("경쟁 호스텔명", key="new_comp_name")
        new_hotel_note = c_add2.text_input("비고 (예: 도미토리 주력, 신축 컨버전 등)", key="new_comp_note")
        with c_add3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("Comp Set 등록", width="stretch"):
                if not serpapi_key:
                    st.error("SerpApi 키가 설정되지 않았습니다. 로컬 `.env` 또는 클라우드 `Secrets` 설정을 확인해 주세요.")
                elif not new_hotel_name.strip():
                    st.warning("숙소명을 입력해 주세요.")
                else:
                    try:
                        with st.spinner("Google Hotels property_token 조회 및 DB 저장 중..."):
                            token, resolved_title = resolve_google_hotel_token(new_hotel_name, serpapi_key)
                            upsert_comp_set_property(new_hotel_name, token, resolved_title, new_hotel_note)
                            st.success(f"등록 완료: {resolved_title}")
                            st.rerun()
                    except Exception as e:
                        st.error(f"등록 실패: {e}")

        saved_comp_df = get_saved_comp_set()
        if not saved_comp_df.empty:
            st.dataframe(saved_comp_df[["입력숙소명", "구글매칭명", "비고", "등록일"]], width="stretch", hide_index=True)
            del_col1, del_col2, del_col3 = st.columns([2.5, 1, 1])
            del_target = del_col1.selectbox("삭제할 숙소 선택", options=saved_comp_df["입력숙소명"].tolist())
            if del_col2.button("선택 숙소 삭제", width="stretch"):
                delete_comp_set_property(del_target)
                st.rerun()
            if del_col3.button("전체 목록 비우기", width="stretch"):
                clear_comp_set_properties()
                st.session_state.pop("latest_comp_snap", None)
                st.rerun()
        else:
            st.info("등록된 Comp Set이 없습니다. 2번 탭에서 숙소를 선택해 등록하거나 위 입력란에 직접 경쟁 호스텔을 등록해 주세요.")

    if not saved_comp_df.empty:
        st.markdown("---")
        col_run1, col_run2, col_run3 = st.columns([1.2, 2.2, 2.1])
        track_days = col_run1.selectbox("향후 조회 기간", options=[14, 21, 30], index=1, format_func=lambda x: f"향후 {x}일")
        with col_run2:
            st.markdown("<div style='font-size:14px;font-weight:500;margin-bottom:6px;'>객실 단가 기준 (1~3개 선택)</div>", unsafe_allow_html=True)
            chk_c1, chk_c2, chk_c3 = st.columns(3)
            g1 = chk_c1.checkbox("1인(도미)", value=True, key="chk_guest_1")
            g2 = chk_c2.checkbox("2인(더블)", value=False, key="chk_guest_2")
            g4 = chk_c3.checkbox("4인(그룹)", value=False, key="chk_guest_4")
            selected_guests = [g for g, ok in [(1, g1), (2, g2), (4, g4)] if ok]
        with col_run3:
            st.markdown("<br>", unsafe_allow_html=True)
            api_calls_est = len(saved_comp_df) * len(selected_guests)
            fetch_cal_btn = st.button(
                f"선택된 {len(saved_comp_df)}개사 가격 캘린더 수집 (API {api_calls_est}회 소모)",
                type="primary",
                width="stretch",
            )

        if fetch_cal_btn:
            if not serpapi_key:
                st.error("SerpApi 키가 설정되지 않았습니다. 로컬 `.env` 또는 클라우드 `Secrets` 설정을 확인해 주세요.")
            elif not selected_guests:
                st.warning("조회할 객실 단가 기준(1인/2인/4인)을 최소 1개 이상 체크해 주세요.")
            else:
                guest_str = "·".join(f"{g}인" for g in selected_guests)
                with st.spinner(f"{guest_str} 기준 Google Hotels 일자별 요금 및 매진 현황을 수집 중입니다..."):
                    try:
                        all_snaps = [
                            fetch_price_calendar_for_comp_set(saved_comp_df, serpapi_key, days_ahead=track_days, adults=ad)
                            for ad in selected_guests
                        ]
                        snap_df = pd.concat([d for d in all_snaps if not d.empty], ignore_index=True) if any(not d.empty for d in all_snaps) else pd.DataFrame()
                        st.session_state["latest_comp_snap"] = snap_df
                    except Exception as e:
                        st.error(f"캘린더 수집 오류: {e}")

        if "latest_comp_snap" not in st.session_state or st.session_state["latest_comp_snap"].empty:
            st.info("👆 원하시는 기준(1인·2인·4인)을 체크한 뒤 파란색 **[가격 캘린더 수집]** 버튼을 누르시면 평일·주말 ADR과 추정 OCC가 즉시 계산됩니다!")
        else:
            snap_df = st.session_state["latest_comp_snap"]

            st.markdown("#### 숙소별 · 인원 기준별(1인·2인·4인) ADR 및 전량마감률·만실임박 비교")
            st.caption("💡 구글 호텔 공개 데이터는 잔여 객실 수(가동률)를 직접 공개하지 않으므로, **전량마감률(100% 품절일 비율)**과 평일 대비 1.8배 이상 요금이 치솟는 **만실임박(가격급등일)**을 함께 보여드립니다.")
            summary_all_df = compute_per_hotel_summary(snap_df)
            if not summary_all_df.empty:
                st.dataframe(
                    summary_all_df.style.format({"평일 ADR(원)": "{:,.0f}", "주말 ADR(원)": "{:,.0f}", "평일 전량마감률(%)": "{:.1f}%", "주말 전량마감률(%)": "{:.1f}%"}, na_rep="-"),
                    width="stretch",
                    hide_index=True,
                )

            def _calc_metrics(df_sub: pd.DataFrame) -> tuple[float, float, float, float, bool]:
                wd = df_sub[df_sub["요일구분"] == "평일(일~목)"]
                we = df_sub[df_sub["요일구분"] == "주말(금·토)"]
                wd_v = wd["노출판매가(원)"].dropna()
                we_v = we["노출판매가(원)"].dropna()
                wd_a = float(wd_v.mean()) if not wd_v.empty else 0.0
                we_a = float(we_v.mean()) if not we_v.empty else (wd_a * 1.35 if wd_a > 0 else 0.0)
                wd_o = float((wd["예약상태"] == "마감/미노출").mean() * 100.0) if not wd.empty else 70.0
                we_o = float((we["예약상태"] == "마감/미노출").mean() * 100.0) if not we.empty else 85.0
                return wd_a, we_a, wd_o, we_o, we_v.empty and wd_a > 0

            # 5번 시뮬레이터 원클릭 반영 (1인->도미토리, 2인->프라이빗룸 동시 반영)
            hotel_names_list = snap_df["경쟁숙소"].dropna().unique().tolist()
            apply_options = ["전체 Comp Set 평균"] + hotel_names_list
            app_c1, app_c2 = st.columns([1.5, 2.5])
            apply_source = app_c1.selectbox("5번 시뮬레이터 반영 기준", options=apply_options, key="comp_apply_source")
            with app_c2:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button(f"📌 [{apply_source}]의 1인(도미토리)·2인(프라이빗룸) 단가/OCC를 5번 시뮬레이터에 즉시 반영", width="stretch"):
                    src_df = snap_df if apply_source == "전체 Comp Set 평균" else snap_df[snap_df["경쟁숙소"] == apply_source]
                    applied_parts = []
                    for ad_cnt, prefix, label in [(1, "sim_dorm", "도미토리(1인)"), (2, "sim_priv", "프라이빗룸(2인)")]:
                        sub_ad = src_df[src_df["기준인원"] == ad_cnt] if "기준인원" in src_df.columns else pd.DataFrame()
                        if not sub_ad.empty:
                            a_wd, a_we, o_wd, o_we, _ = _calc_metrics(sub_ad)
                            if a_wd > 0:
                                st.session_state[f"{prefix}_wd_adr"] = int(round(a_wd, -2))
                                st.session_state[f"{prefix}_we_adr"] = int(round(a_we, -2))
                                # 전량마감률이 30% 미만(잔여객실이 1실 이상 남아있는 다객실 호텔 등)이면 기존 가동률 가정 유지
                                if o_wd >= 30:
                                    st.session_state[f"{prefix}_wd_occ"] = int(min(98, round(o_wd)))
                                if o_we >= 30:
                                    st.session_state[f"{prefix}_we_occ"] = int(min(98, round(o_we)))
                                applied_parts.append(label)
                    if applied_parts:
                        st.success(f"[{apply_source}]의 {', '.join(applied_parts)} 단가·가동률이 5번 탭에 반영되었습니다.")
                    else:
                        st.warning("반영할 수 있는 유효 판매가 데이터가 없습니다.")

            # 1인 / 2인 / 4인 상세 탭 3개 기본 표시
            ad_tab1, ad_tab2, ad_tab4 = st.tabs(["1인 기준 (도미토리 베드)", "2인 기준 (더블룸)", "4인 기준 (패밀리/그룹)"])
            for ad_cnt, ad_tab in [(1, ad_tab1), (2, ad_tab2), (4, ad_tab4)]:
                with ad_tab:
                    sub_df = snap_df[snap_df["기준인원"] == ad_cnt] if "기준인원" in snap_df.columns else snap_df
                    if sub_df.empty:
                        st.info(f"{ad_cnt}인 기준 수집 데이터가 없습니다. 위 일괄 수집 버튼을 다시 눌러주세요.")
                    else:
                        m_wd_a, m_we_a, m_wd_o, m_we_o, is_est = _calc_metrics(sub_df)
                        s1, s2, s3, s4 = st.columns(4)
                        s1.metric(f"{ad_cnt}인 평일(일~목) ADR", f"{m_wd_a:,.0f} 원")
                        s2.metric(f"{ad_cnt}인 주말(금·토) ADR", f"{m_we_a:,.0f} 원" + (" (추정)" if is_est else ""))
                        s3.metric(f"{ad_cnt}인 평일 전량마감률", f"{m_wd_o:.1f} %")
                        s4.metric(f"{ad_cnt}인 주말 전량마감률", f"{m_we_o:.1f} %")

                        st.markdown(f"##### {ad_cnt}인 기준 체크인 일자별 판매가 매트릭스 (원)")
                        piv = sub_df.pivot(index="체크인일자", columns="경쟁숙소", values="노출판매가(원)")
                        piv = piv.apply(pd.to_numeric, errors="coerce")
                        st.dataframe(piv.style.format("{:,.0f}", na_rep="매진/미판매"), width="stretch")

        with st.expander("📂 로컬 DB 누적 스냅샷 히스토리 (최근 30일)"):
            hist_df = load_historical_snapshots(30)
            if not hist_df.empty:
                st.dataframe(hist_df, width="stretch", hide_index=True, height=240)
                st.download_button(
                    "히스토리 CSV 백업 다운로드",
                    data=hist_df.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"comp_set_history_{datetime.date.today().strftime('%Y%m%d')}.csv",
                    mime="text/csv",
                )

# ---------------------------------------------------------
# [TAB 5] 임차(마스터리스) 정밀 수지 시뮬레이터
# ---------------------------------------------------------
with tab5:
    st.subheader("임차(마스터리스) 호스텔 정밀 수지 시뮬레이터 (평일·주말 및 객실 믹스 분리)")

    default_py = 200.0
    if "analysis_result" in st.session_state:
        titles_list = st.session_state["analysis_result"]["bld_data"].get("title") or []
        area_val = float((titles_list[0].get("totArea") if titles_list else 0) or 0)
        if area_val > 0:
            default_py = round(area_val * 0.3025, 1)

    col_top1, col_top2 = st.columns(2)
    with col_top1:
        st.markdown("**① 임대차·CAPEX 및 계절성 보정**")
        c1a, c1b = st.columns(2)
        lease_py = c1a.number_input("임차 전체 면적 (평)", value=float(default_py), step=10.0)
        lease_years = c1b.slider("임차 계약기간 (년)", min_value=3, max_value=10, value=7)
        deposit_man = c1a.number_input("임대보증금 (만원)", value=30000, step=5000)
        monthly_rent_man = c1b.number_input("월 임대료+관리비 (만원)", value=2200, step=100)
        capex_per_py_man = c1a.number_input("평당 컨버전 공사비 (만원/평)", value=450, step=50)
        seasonality_factor = c1b.slider(
            "연평균 계절성 보정계수",
            min_value=0.75, max_value=1.05, value=0.92, step=0.01,
            help="성수기 단기 스냅샷 반영 시 비수기(1~2월, 7~8월) 하락분을 반영하는 보수적 보정계수",
        )

    with col_top2:
        st.markdown("**② 공간 배분 및 객실 믹스 설계 (공용 라운지·복도 포함 기준)**")
        dorm_area_pct = st.slider("도미토리(다인실) 구역 면적 비중 (%)", min_value=0, max_value=100, value=60, step=5)
        priv_area_pct = 100 - dorm_area_pct
        c2a, c2b = st.columns(2)
        py_per_dorm_bed = c2a.number_input("도미토리 1베드당 소요 면적 (평)", value=2.5, step=0.1, min_value=1.5)
        py_per_priv_room = c2b.number_input("프라이빗룸 1실당 소요 면적 (평)", value=6.5, step=0.5, min_value=3.5)

        dorm_area_py = lease_py * (dorm_area_pct / 100.0)
        priv_area_py = lease_py * (priv_area_pct / 100.0)
        est_dorm_beds = int(dorm_area_py // py_per_dorm_bed) if dorm_area_pct > 0 else 0
        est_priv_rooms = int(priv_area_py // py_per_priv_room) if priv_area_pct > 0 else 0
        total_sellable_units = max(1, est_dorm_beds + est_priv_rooms)

        st.info(
            f"📐 **산출 객실 구성:** 도미토리 **{est_dorm_beds}베드** ({dorm_area_py:,.1f}평, {dorm_area_pct}%) + "
            f"프라이빗룸 **{est_priv_rooms}실** ({priv_area_py:,.1f}평, {priv_area_pct}%) = "
            f"**총 {total_sellable_units} 판매단위(Unit)**"
        )

    st.markdown("---")

    col_mid1, col_mid2, col_mid3 = st.columns(3)
    with col_mid1:
        st.markdown("**③ 도미토리 (1베드 기준) 단가·가동률**")
        dorm_wd_adr = st.number_input("도미토리 평일(일~목) ADR (원)", step=1000, key="sim_dorm_wd_adr")
        dorm_we_adr = st.number_input("도미토리 주말(금·토) ADR (원)", step=1000, key="sim_dorm_we_adr")
        dorm_wd_occ = st.slider("도미토리 평일 가동률 (%)", min_value=30, max_value=98, key="sim_dorm_wd_occ")
        dorm_we_occ = st.slider("도미토리 주말 가동률 (%)", min_value=30, max_value=98, key="sim_dorm_we_occ")

    with col_mid2:
        st.markdown("**④ 프라이빗룸 (1실 기준) 단가·가동률**")
        priv_wd_adr = st.number_input("프라이빗룸 평일(일~목) ADR (원)", step=2000, key="sim_priv_wd_adr")
        priv_we_adr = st.number_input("프라이빗룸 주말(금·토) ADR (원)", step=2000, key="sim_priv_we_adr")
        priv_wd_occ = st.slider("프라이빗룸 평일 가동률 (%)", min_value=30, max_value=98, key="sim_priv_wd_occ")
        priv_we_occ = st.slider("프라이빗룸 주말 가동률 (%)", min_value=30, max_value=98, key="sim_priv_we_occ")

    with col_mid3:
        st.markdown("**⑤ 운영비(OPEX) 및 수수료 가정**")
        ota_fee_pct = st.slider("OTA 수수료 및 PG율 (%)", min_value=10, max_value=22, value=16)
        staff_cost_man = st.number_input("월 고정 인건비 (만원)", value=1400, step=100)
        var_cost_per_night = st.number_input(
            "판매 1박당 변동비 (원/박)", value=3500, step=500,
            help="린넨 세탁비, 객실 소모품·어메니티 등 가동률 연동 원가",
        )
        fixed_etc_opex_man = st.number_input("월 수도광열·보험·마케팅 고정비 (만원)", value=450, step=50)

    WD_DAYS = 365.0 / 12.0 * (5.0 / 7.0)
    WE_DAYS = 365.0 / 12.0 * (2.0 / 7.0)

    dorm_wd_nights = est_dorm_beds * WD_DAYS * (dorm_wd_occ / 100.0)
    dorm_we_nights = est_dorm_beds * WE_DAYS * (dorm_we_occ / 100.0)
    dorm_wd_rev_krw = dorm_wd_nights * dorm_wd_adr * seasonality_factor
    dorm_we_rev_krw = dorm_we_nights * dorm_we_adr * seasonality_factor

    priv_wd_nights = est_priv_rooms * WD_DAYS * (priv_wd_occ / 100.0)
    priv_we_nights = est_priv_rooms * WE_DAYS * (priv_we_occ / 100.0)
    priv_wd_rev_krw = priv_wd_nights * priv_wd_adr * seasonality_factor
    priv_we_rev_krw = priv_we_nights * priv_we_adr * seasonality_factor

    total_sold_nights = dorm_wd_nights + dorm_we_nights + priv_wd_nights + priv_we_nights
    total_available_nights = total_sellable_units * (WD_DAYS + WE_DAYS)
    gross_monthly_rev_krw = dorm_wd_rev_krw + dorm_we_rev_krw + priv_wd_rev_krw + priv_we_rev_krw
    gross_monthly_rev_man = gross_monthly_rev_krw / 10000.0

    weighted_adr_krw = (gross_monthly_rev_krw / total_sold_nights) if total_sold_nights > 0 else 0.0
    blended_occ_pct = (total_sold_nights / total_available_nights * 100.0) if total_available_nights > 0 else 0.0
    blended_revpar_krw = weighted_adr_krw * (blended_occ_pct / 100.0)

    net_monthly_rev_man = gross_monthly_rev_man * (1.0 - ota_fee_pct / 100.0)
    monthly_var_opex_man = (total_sold_nights * var_cost_per_night) / 10000.0
    monthly_fixed_opex_man = monthly_rent_man + staff_cost_man + fixed_etc_opex_man
    total_monthly_opex_man = monthly_fixed_opex_man + monthly_var_opex_man

    monthly_ebitda_man = net_monthly_rev_man - total_monthly_opex_man
    annual_ebitda_man = monthly_ebitda_man * 12.0
    total_capex_man = lease_py * capex_per_py_man
    payback_years = (total_capex_man / annual_ebitda_man) if annual_ebitda_man > 0 else 99.9

    contribution_margin_ratio = (
        (net_monthly_rev_man - monthly_var_opex_man) / gross_monthly_rev_man
        if gross_monthly_rev_man > 0 else 0.0
    )
    bep_gross_rev_man = (monthly_fixed_opex_man / contribution_margin_ratio) if contribution_margin_ratio > 0 else 999999.0
    bep_occ_pct = (
        blended_occ_pct * (bep_gross_rev_man / gross_monthly_rev_man)
        if gross_monthly_rev_man > 0 else 99.9
    )

    st.markdown("---")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("실판매 가중평균 ADR", f"{weighted_adr_krw:,.0f} 원", f"RevPAR {blended_revpar_krw:,.0f}원")
    k2.metric("통합 가동률 (Blended OCC)", f"{blended_occ_pct:.1f} %", f"BEP OCC {bep_occ_pct:.1f}%")
    k3.metric("월 총매출 / 순매출(OTA후)", f"{gross_monthly_rev_man:,.0f}만 / {net_monthly_rev_man:,.0f}만")
    k4.metric("월 영업현금흐름(EBITDA)", f"{monthly_ebitda_man:,.0f} 만원", f"연 {annual_ebitda_man:,.0f}만원")
    k5.metric("CAPEX 회수기간(Payback)", f"{payback_years:.1f} 년", f"총 CAPEX {total_capex_man:,.0f}만")

    if payback_years > lease_years * 0.6:
        st.warning(f"⚠️ **임차 회수 리스크:** CAPEX 회수기간({payback_years:.1f}년)이 전체 임차기간({lease_years}년)의 60%를 초과합니다. 평일 가동률 방어 또는 프라이빗룸 비중 확대를 통한 단가 상향이 필요합니다.")
    else:
        st.success(f"✅ **회수기간 적정:** 임차기간 {lease_years}년 대비 약 {payback_years:.1f}년 내 초기 시설투자비 회수가 가능합니다.")

    st.markdown("#### 상품별 · 요일별 월간 매출 기여도 및 평당 효율 비교")
    breakdown_df = pd.DataFrame([
        {
            "상품구분": "도미토리 (베드)",
            "배분면적(평)": round(dorm_area_py, 1),
            "공급단위": f"{est_dorm_beds} 베드",
            "평일매출(만원)": round(dorm_wd_rev_krw / 10000.0, 1),
            "주말매출(만원)": round(dorm_we_rev_krw / 10000.0, 1),
            "월합계매출(만원)": round((dorm_wd_rev_krw + dorm_we_rev_krw) / 10000.0, 1),
            "매출비중(%)": round(((dorm_wd_rev_krw + dorm_we_rev_krw) / gross_monthly_rev_krw * 100.0) if gross_monthly_rev_krw > 0 else 0, 1),
            "평당월매출(만원/평)": round(((dorm_wd_rev_krw + dorm_we_rev_krw) / 10000.0 / dorm_area_py) if dorm_area_py > 0 else 0, 1),
        },
        {
            "상품구분": "프라이빗룸 (객실)",
            "배분면적(평)": round(priv_area_py, 1),
            "공급단위": f"{est_priv_rooms} 실",
            "평일매출(만원)": round(priv_wd_rev_krw / 10000.0, 1),
            "주말매출(만원)": round(priv_we_rev_krw / 10000.0, 1),
            "월합계매출(만원)": round((priv_wd_rev_krw + priv_we_rev_krw) / 10000.0, 1),
            "매출비중(%)": round(((priv_wd_rev_krw + priv_we_rev_krw) / gross_monthly_rev_krw * 100.0) if gross_monthly_rev_krw > 0 else 0, 1),
            "평당월매출(만원/평)": round(((priv_wd_rev_krw + priv_we_rev_krw) / 10000.0 / priv_area_py) if priv_area_py > 0 else 0, 1),
        },
    ])
    st.dataframe(breakdown_df, width="stretch", hide_index=True)

    st.markdown("#### 월간 손익 구조 한눈에 보기 (단위: 만원)")
    pnl_chart_df = pd.DataFrame({
        "항목": ["1. 월 총매출", "2. 순매출(OTA후)", "3. 임대료", "4. 인건비·기타고정", "5. 변동비", "6. 영업현금흐름(EBITDA)"],
        "금액(만원)": [
            round(gross_monthly_rev_man, 1),
            round(net_monthly_rev_man, 1),
            round(monthly_rent_man, 1),
            round(staff_cost_man + fixed_etc_opex_man, 1),
            round(monthly_var_opex_man, 1),
            round(monthly_ebitda_man, 1),
        ],
    }).set_index("항목")
    st.bar_chart(pnl_chart_df, height=260)

    sim_summary_dict = {
        "임차면적(평)": lease_py,
        "임대보증금(만원)": deposit_man,
        "도미토리베드수": est_dorm_beds,
        "프라이빗룸실수": est_priv_rooms,
        "실판매가중평균ADR(원)": round(weighted_adr_krw, 0),
        "통합OCC(%)": round(blended_occ_pct, 1),
        "계절성보정계수": seasonality_factor,
        "초기CAPEX(만원)": total_capex_man,
        "월총매출(만원)": round(gross_monthly_rev_man, 1),
        "월순매출_OTA차감후(만원)": round(net_monthly_rev_man, 1),
        "월총운영비(만원)": round(total_monthly_opex_man, 1),
        "월영업현금흐름_EBITDA(만원)": round(monthly_ebitda_man, 1),
        "CAPEX회수기간(년)": round(payback_years, 2),
        "BEP가동률(%)": round(bep_occ_pct, 1),
    }
    st.session_state["latest_sim_summary"] = sim_summary_dict
    st.session_state["latest_breakdown_df"] = breakdown_df

    excel_bytes = build_full_excel_bytes(
        st.session_state.get("analysis_result"),
        get_saved_comp_set(),
        st.session_state.get("latest_comp_snap"),
        sim_summary_dict,
        breakdown_df,
    )
    st.download_button(
        label="전체 분석 결과(1~5번 탭 통합) 엑셀(.xlsx) 다운로드",
        data=excel_bytes,
        file_name=f"서울_호스텔_수지검토_{datetime.date.today().strftime('%Y%m%d')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
