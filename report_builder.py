import datetime
import html
from io import BytesIO
import pandas as pd


def compute_per_hotel_summary(snap_df: pd.DataFrame) -> pd.DataFrame:
    """가격 캘린더 스냅샷에서 숙소별 평일·주말 ADR과 추정 OCC 요약 표를 생성합니다."""
    if snap_df is None or snap_df.empty:
        return pd.DataFrame()
    rows = []
    for h_name, grp in snap_df.groupby("경쟁숙소", sort=False):
        h_wd = grp[grp["요일구분"] == "평일(일~목)"]
        h_we = grp[grp["요일구분"] == "주말(금·토)"]
        h_wd_valid = h_wd["노출판매가(원)"].dropna()
        h_we_valid = h_we["노출판매가(원)"].dropna()
        h_wd_adr = float(h_wd_valid.mean()) if not h_wd_valid.empty else 0.0
        h_we_adr = float(h_we_valid.mean()) if not h_we_valid.empty else (h_wd_adr * 1.35 if h_wd_adr > 0 else 0.0)
        h_wd_occ = float((h_wd["예약상태"] == "마감/미노출").mean() * 100.0) if not h_wd.empty else 0.0
        h_we_occ = float((h_we["예약상태"] == "마감/미노출").mean() * 100.0) if not h_we.empty else 0.0
        open_cnt = int(grp["노출판매가(원)"].notna().sum())
        rows.append({
            "경쟁숙소": h_name,
            "평일 ADR(원)": round(h_wd_adr) if h_wd_adr > 0 else None,
            "주말 ADR(원)": round(h_we_adr) if h_we_adr > 0 else None,
            "평일 추정 OCC(%)": round(h_wd_occ, 1),
            "주말 추정 OCC(%)": round(h_we_occ, 1),
            "판매노출일수": f"{open_cnt} / {len(grp)}일",
        })
    return pd.DataFrame(rows)


def build_full_excel_bytes(
    analysis_result: dict | None,
    comp_df: pd.DataFrame,
    comp_snap_df: pd.DataFrame | None,
    sim_summary: dict | None,
    breakdown_df: pd.DataFrame | None,
) -> bytes:
    """1~5번 탭 전체 검토 내용을 하나의 통합 엑셀(.xlsx) 바이트로 생성합니다."""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        meta_row = {
            "보고서작성일": datetime.date.today().strftime("%Y-%m-%d"),
            "후보건물주소": (analysis_result or {}).get("geo", {}).get("address_name", "미조회 (스크리닝 실행 전)"),
            "등록된CompSet수": len(comp_df) if comp_df is not None else 0,
            "수지시뮬레이션포함": "예" if sim_summary else "아니오 (5번 탭 확인 후 재저장)",
        }
        pd.DataFrame([meta_row]).to_excel(writer, sheet_name="00_검토개요", index=False)

        if analysis_result:
            bld_data = analysis_result.get("bld_data", {})
            titles_data = bld_data.get("title", [])
            if titles_data:
                pd.DataFrame([titles_data[0]]).to_excel(writer, sheet_name="01_건축물표제부", index=False)
            floors_data = bld_data.get("floors", [])
            if floors_data:
                pd.DataFrame(floors_data).to_excel(writer, sheet_name="01_2_층별개요", index=False)
            subway_df = analysis_result.get("subway_df", pd.DataFrame())
            if not subway_df.empty:
                subway_df.to_excel(writer, sheet_name="02_역세권분석", index=False)
            tour_df = analysis_result.get("tour_df", pd.DataFrame())
            if not tour_df.empty:
                tour_df.to_excel(writer, sheet_name="02_2_관광공사POI", index=False)
            kakao_df = analysis_result.get("kakao_lodging_df", pd.DataFrame())
            if not kakao_df.empty:
                kakao_df.to_excel(writer, sheet_name="03_반경내숙박_카카오", index=False)
            mois_t = analysis_result.get("mois_df", pd.DataFrame())
            mois_g = analysis_result.get("mois_gen_df", pd.DataFrame())
            mois_all = pd.concat([d for d in (mois_t, mois_g) if not d.empty], ignore_index=True) if (not mois_t.empty or not mois_g.empty) else pd.DataFrame()
        if not mois_all.empty:
            mois_all["_dk"] = mois_all["사업장명"].astype(str).str.replace(" ", "", regex=False).str.lower()
            mois_all = mois_all.sort_values("거리(m)").drop_duplicates(subset=["_dk"], keep="first").drop(columns=["_dk"])
            if not mois_all.empty:
                mois_all["_dk"] = mois_all["사업장명"].astype(str).str.replace(" ", "", regex=False).str.lower()
                mois_all = mois_all.sort_values("거리(m)").drop_duplicates(subset=["_dk"], keep="first").drop(columns=["_dk"])
                mois_all.to_excel(writer, sheet_name="03_2_행안부인허가숙박", index=False)

        if comp_df is not None and not comp_df.empty:
            comp_df.drop(columns=["property_token"], errors="ignore").to_excel(writer, sheet_name="04_CompSet_등록목록", index=False)
        if comp_snap_df is not None and not comp_snap_df.empty:
            per_hotel_df = compute_per_hotel_summary(comp_snap_df)
            if not per_hotel_df.empty:
                per_hotel_df.to_excel(writer, sheet_name="04_2_CompSet_숙소별요약", index=False)
            comp_snap_df.to_excel(writer, sheet_name="04_3_CompSet_가격캘린더", index=False)

        if breakdown_df is not None and not breakdown_df.empty:
            breakdown_df.to_excel(writer, sheet_name="05_객실믹스_요일별매출", index=False)
        if sim_summary:
            pd.DataFrame([sim_summary]).to_excel(writer, sheet_name="06_임차수지총괄", index=False)

    return output.getvalue()


def _df_to_html(df: pd.DataFrame, max_rows: int = 25) -> str:
    if df is None or df.empty:
        return "<p class='empty'>조회된 데이터가 없습니다.</p>"
    return df.head(max_rows).to_html(index=False, classes="report-table", border=0, na_rep="-")


def build_printable_html_report(
    analysis_result: dict | None,
    comp_df: pd.DataFrame,
    comp_snap_df: pd.DataFrame | None,
    sim_summary: dict | None,
    breakdown_df: pd.DataFrame | None,
) -> str:
    """1~5번 탭 전체 검토 내용을 A4 인쇄 및 PDF 저장에 최적화된 HTML 보고서로 생성합니다."""
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    sec1_html = "<p class='empty'>입지 스크리닝을 실행한 후 출력하시면 건축물대장·상권 데이터가 포함됩니다.</p>"
    sec2_html = "<p class='empty'>입지 스크리닝 실행 후 역세권·상권·숙박 데이터가 표시됩니다.</p>"

    if analysis_result:
        geo = analysis_result.get("geo", {})
        addr = html.escape(str(geo.get("address_name", "")))
        bld = analysis_result.get("bld_data", {})
        titles = bld.get("title", [])
        main_t = titles[0] if titles else {}
        bld_nm = html.escape(str(main_t.get("bldNm") or "건물명 미등재"))
        purps = html.escape(str(main_t.get("mainPurpsCdNm") or "정보없음"))
        tot_area = float(main_t.get("totArea") or 0)
        flr_info = f"지하{main_t.get('ugrndFlrCnt') or '-'}/지상{main_t.get('grndFlrCnt') or '-'}층"
        vltn = "🚨 위반건축물 등재" if str(main_t.get("vltnBldYn") or "0") == "1" else "✅ 위반건축물 미등재"
        schools = analysis_result.get("schools_200m", [])
        school_str = f"⚠️ {len(schools)}개소 ({', '.join(s['place_name'] for s in schools)})" if schools else "✅ 반경 200m 내 학교 없음"
        zoning = ", ".join({str(z.get("jijiguCdNm")) for z in bld.get("zoning", []) if z.get("jijiguCdNm")}) or "미등재"

        floors = bld.get("floors", [])
        flr_df = pd.DataFrame([{
            "층구분": f.get("flrGbCdNm", ""),
            "층수": f.get("flrNoNm", ""),
            "주용도": f.get("mainPurpsCdNm", ""),
            "면적(㎡)": float(f.get("area") or 0),
            "면적(평)": round(float(f.get("area") or 0) * 0.3025, 1),
        } for f in floors]) if floors else pd.DataFrame()

        sec1_html = f"""
        <div class="kpi-grid">
            <div class="kpi"><span>대상 건물</span><b>{bld_nm}</b><small>{addr}</small></div>
            <div class="kpi"><span>현재 주용도</span><b>{purps}</b><small>{flr_info}</small></div>
            <div class="kpi"><span>연면적</span><b>{tot_area:,.1f} ㎡</b><small>{tot_area * 0.3025:,.1f} 평</small></div>
            <div class="kpi"><span>위반 / 학교체크</span><b>{vltn}</b><small>{html.escape(school_str)}</small></div>
        </div>
        <p class="note"><b>등재 용도지역·지구:</b> {html.escape(zoning)}</p>
        <h4>층별 개요</h4>
        {_df_to_html(flr_df, 20)}
        """

        subway_df = analysis_result.get("subway_df", pd.DataFrame())
        tour_df = analysis_result.get("tour_df", pd.DataFrame())
        poi_non = tour_df[tour_df["contentTypeId"] != "32"] if not tour_df.empty else pd.DataFrame()
        kakao_df = analysis_result.get("kakao_lodging_df", pd.DataFrame())
        k_cols = [c for c in ["업소명", "세부분류", "거리(m)", "준공년월", "건물규모", "주소"] if c in kakao_df.columns]
        mois_t = analysis_result.get("mois_df", pd.DataFrame())
        mois_g = analysis_result.get("mois_gen_df", pd.DataFrame())
        mois_all = pd.concat([d for d in (mois_t, mois_g) if not d.empty], ignore_index=True) if (not mois_t.empty or not mois_g.empty) else pd.DataFrame()
        if not mois_all.empty:
            mois_all["_dk"] = mois_all["사업장명"].astype(str).str.replace(" ", "", regex=False).str.lower()
            mois_all = mois_all.sort_values("거리(m)").drop_duplicates(subset=["_dk"], keep="first").drop(columns=["_dk"])
        m_cols = [c for c in ["사업장명", "업태구분", "영업시작일", "객실수(실)", "거리(m)", "도로명주소"] if c in mois_all.columns]

        nearest_sub = f"{subway_df.iloc[0]['역명']} ({subway_df.iloc[0]['거리(m)']}m)" if not subway_df.empty else "없음"
        sec2_html = f"""
        <div class="kpi-grid">
            <div class="kpi"><span>최인접 지하철역</span><b>{html.escape(nearest_sub)}</b></div>
            <div class="kpi"><span>반경 내 지하철역</span><b>{len(subway_df)} 개역</b></div>
            <div class="kpi"><span>관광공사 집객 POI</span><b>{len(poi_non)} 건</b></div>
            <div class="kpi"><span>반경 내 숙박업소</span><b>카카오 {len(kakao_df)} / 인허가 {len(mois_all)}</b></div>
        </div>
        <h4>반경 내 숙박업소 (카카오 로컬 기준 상위 15)</h4>
        {_df_to_html(kakao_df[k_cols] if k_cols else pd.DataFrame(), 15)}
        <h4>행안부 인허가 숙박업소 (영업시작일 · 객실수 기준 상위 15)</h4>
        {_df_to_html(mois_all.sort_values("거리(m)")[m_cols] if m_cols else pd.DataFrame(), 15)}
        """

    per_hotel_df = compute_per_hotel_summary(comp_snap_df) if comp_snap_df is not None else pd.DataFrame()
    comp_disp = comp_df[["입력숙소명", "구글매칭명", "비고"]] if (comp_df is not None and not comp_df.empty) else pd.DataFrame()
    sec4_html = f"""
    <h4>등록된 Comp Set 목록</h4>
    {_df_to_html(comp_disp, 15)}
    <h4>숙소별 평일 · 주말 ADR 및 추정 OCC</h4>
    {_df_to_html(per_hotel_df, 15)}
    """

    sec5_html = "<p class='empty'>수지 시뮬레이터 데이터가 없습니다.</p>"
    if sim_summary:
        sec5_html = f"""
        <div class="kpi-grid">
            <div class="kpi"><span>객실 구성</span><b>도미 {sim_summary.get('도미토리베드수', 0)}베드 + 프라이빗 {sim_summary.get('프라이빗룸실수', 0)}실</b><small>임차 {sim_summary.get('임차면적(평)', 0)}평</small></div>
            <div class="kpi"><span>가중평균 ADR / OCC</span><b>{sim_summary.get('실판매가중평균ADR(원)', 0):,.0f} 원</b><small>OCC {sim_summary.get('통합OCC(%)', 0)}% (BEP {sim_summary.get('BEP가동률(%)', 0)}%)</small></div>
            <div class="kpi"><span>월 총매출 / 순매출</span><b>{sim_summary.get('월총매출(만원)', 0):,.0f} 만원</b><small>OTA 후 {sim_summary.get('월순매출_OTA차감후(만원)', 0):,.0f} 만원</small></div>
            <div class="kpi"><span>월 EBITDA / 회수기간</span><b>{sim_summary.get('월영업현금흐름_EBITDA(만원)', 0):,.0f} 만원</b><small>Payback {sim_summary.get('CAPEX회수기간(년)', 0)}년 (CAPEX {sim_summary.get('초기CAPEX(만원)', 0):,.0f}만)</small></div>
        </div>
        <h4>상품별 · 요일별 매출 기여도</h4>
        {_df_to_html(breakdown_df if breakdown_df is not None else pd.DataFrame(), 10)}
        """

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>서울 호스텔 시장조사 및 수지검토 보고서 ({today_str})</title>
<style>
  body {{ font-family: 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif; color: #0F172A; margin: 24px; line-height: 1.5; }}
  .toolbar {{ background: #EFF6FF; border: 1px solid #BFDBFE; padding: 12px 16px; border-radius: 10px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; }}
  .print-btn {{ background: #2563EB; color: #fff; border: none; padding: 9px 18px; border-radius: 8px; font-weight: 700; cursor: pointer; font-size: 14px; }}
  .header {{ background: linear-gradient(135deg, #0F172A, #1E3A8A); color: #fff; padding: 18px 24px; border-radius: 12px; margin-bottom: 20px; }}
  .header h1 {{ margin: 0; font-size: 20px; }}
  .header p {{ margin: 4px 0 0; font-size: 12px; color: #DBEAFE; }}
  h2 {{ font-size: 16px; border-left: 4px solid #2563EB; padding-left: 10px; margin-top: 24px; color: #1E3A8A; }}
  h4 {{ font-size: 13px; margin: 12px 0 6px; color: #334155; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 10px 0; }}
  .kpi {{ border: 1px solid #CBD5E1; border-radius: 8px; padding: 10px 12px; background: #F8FAFC; }}
  .kpi span {{ display: block; font-size: 11px; color: #64748B; }}
  .kpi b {{ display: block; font-size: 14px; color: #0F172A; margin-top: 2px; }}
  .kpi small {{ display: block; font-size: 11px; color: #475569; margin-top: 2px; }}
  .report-table {{ width: 100%; border-collapse: collapse; font-size: 11px; margin-bottom: 12px; }}
  .report-table th {{ background: #F1F5F9; border: 1px solid #CBD5E1; padding: 6px; text-align: left; font-weight: 700; }}
  .report-table td {{ border: 1px solid #E2E8F0; padding: 5px 6px; }}
  .note {{ font-size: 12px; background: #F8FAFC; padding: 8px 10px; border-radius: 6px; border: 1px solid #E2E8F0; }}
  .empty {{ font-size: 12px; color: #64748B; }}
  @media print {{
    .no-print {{ display: none !important; }}
    body {{ margin: 10mm; }}
    h2 {{ page-break-after: avoid; }}
    .kpi-grid {{ page-break-inside: avoid; }}
  }}
</style>
</head>
<body>
  <div class="toolbar no-print">
    <span>📄 <b>인쇄 / PDF 저장 안내:</b> 오른쪽 버튼을 누르신 뒤 프린터 대상에서 <b>'PDF로 저장'</b>을 선택하시면 깔끔한 PDF 파일로 저장됩니다.</span>
    <button class="print-btn" onclick="window.print()">🖨️ 인쇄 / PDF로 저장</button>
  </div>
  <div class="header">
    <h1>🏨 서울 호스텔 시장조사 및 수지검토 통합 보고서</h1>
    <p>작성일자: {today_str} | 건축물대장 인허가 · 상권/숙박 · Comp Set · 마스터리스 수지 종합</p>
  </div>

  <h2>1. 용도변경 인허가 · 물리적 진단</h2>
  {sec1_html}

  <h2>2. 역세권 · 상권 및 반경 내 경쟁 숙박</h2>
  {sec2_html}

  <h2>3. 직접 경쟁군(Comp Set) ADR · 추정 OCC</h2>
  {sec4_html}

  <h2>4. 임차(마스터리스) 정밀 수지 검토</h2>
  {sec5_html}
</body>
</html>"""
