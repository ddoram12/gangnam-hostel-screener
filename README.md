# 강남 호스텔 임차 컨버전(용도변경) 입지·CompSet·수지 스크리너

서울 강남권 호스텔 운영 검토를 위한 입지 진단, 상권·역세권 분석, 지도 시각화, 경쟁사(Comp Set) 단가·가동률 추적, 그리고 마스터리스 수지 시뮬레이션을 제공하는 Streamlit 웹앱입니다.

## 주요 기능
1. **용도변경 인허가·물리적 진단**: 건축물대장(표제부·층별개요·정화조·지역지구), 위반건축물 여부, 반경 200m 학교(교육환경보호구역) 체크, 강남권 용도지역 적합도 자동 판정
2. **상권 집객 & 역세권·경쟁 숙박**: 최인접 지하철역 도보 거리, 한국관광공사 집객 POI 분포, 행안부 인허가 관광숙박업, 카카오 로컬 숙박업소 조회
3. **입지 지도 시각화**: 후보지, 지하철역, 200m 학교 보호구역, POI, 경쟁 숙박업소 통합 지도
4. **직접 경쟁군(Comp Set) ADR·OCC 추적**: Google Hotels 기반 평일/주말 단가 및 마감률 수집 + SQLite 누적 + CSV 백업
5. **임차(마스터리스) 정밀 수지 시뮬레이터**: 도미토리/프라이빗룸 믹스, 월 EBITDA, CAPEX 회수기간(Payback), BEP 가동률 계산 및 엑셀(.xlsx) 다운로드

## 클라우드 배포 방법 (Streamlit Community Cloud)
1. 이 폴더의 파일(`app.py`, `api_clients.py`, `db_manager.py`, `requirements.txt`, `.streamlit/config.toml`)을 GitHub 저장소에 업로드합니다. (`.env`, `.streamlit/secrets.toml`, `*.db`는 업로드하지 마세요.)
2. [share.streamlit.io](https://share.streamlit.io) 접속 후 **Create app** 클릭
3. GitHub 저장소와 `Main file path: app.py` 선택
4. **Advanced settings → Secrets**에 아래 형식으로 API 키 입력:
   ```toml
   DATA_GO_KR_KEY = "공공데이터포털_인증키"
   KAKAO_REST_KEY = "카카오_REST_API_키"
   SERPAPI_KEY = "SerpApi_키"
   ```
5. **Deploy** 클릭 후 생성된 웹 주소(`https://...streamlit.app`)로 접속합니다.
