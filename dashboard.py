# -*- coding: utf-8 -*-
"""
경북 학교 현황 대시보드 v4
추가: 학교알리미 학생수 통합 (KPI·지도 마커 크기·학생수 차트·소규모학교 필터)

준비물:
    pip install streamlit plotly folium streamlit-folium requests
    1) gyeongbuk_schools.csv          (나이스 수집본, 같은 폴더)
    2) school_locations.csv           (data.go.kr '전국초중등학교위치표준데이터', 같은 폴더)
    3) alimi_gyeongbuk_62.csv         (학교알리미 수집본, alimi_api_test.py로 생성)
    4) .streamlit/secrets.toml 에 NEIS_KEY 입력 (급식·학사일정용)

실행:  python -m streamlit run dashboard.py
"""

import re
import datetime as dt

import requests
import pandas as pd
import streamlit as st
import plotly.express as px
import folium
from streamlit_folium import st_folium

# ══════════════════════════════════════════════════════════
# [역할 ①] 페이지 설정 + 상수
# ══════════════════════════════════════════════════════════
st.set_page_config(page_title="경북 학교 현황", page_icon="🏫", layout="wide")

# 버전 표식: 사이드바에 표시되어 '지금 어떤 코드가 실행 중인지' 즉시 확인 가능
# (파일 교체 누락 사고 방지 — 수정할 때마다 숫자를 올릴 것)
VERSION = "v4.1 (동명이교 좌표 오배치 수정)"

# ── API 키 읽기: 비밀과 코드의 분리 ──
# 1순위: .streamlit/secrets.toml 의 NEIS_KEY  (배포·GitHub 공개 시 안전)
# 2순위: 아래 빈 문자열 자리에 직접 입력       (로컬에서 빠르게 쓸 때)
# 원리: 코드는 공개해도 되지만 인증키는 공개되면 안 되므로,
#       키를 별도 파일(secrets.toml)에 두고 코드는 '읽는 방법'만 안다.
try:
    API_KEY = st.secrets["NEIS_KEY"]
except Exception:
    API_KEY = ""          # ← 로컬 간편 사용 시 여기에 인증키 입력
OFFICE_CODE = "R10"   # 경상북도교육청

GEO_CSV = "school_locations.csv"   # 위치표준데이터 파일명
ALIMI_CSV = "alimi_gyeongbuk_62.csv"   # 학교알리미 학교현황(62) 수집본
SMALL_MAX = 60                     # 소규모학교 기준: 전교생 60명 이하

# 학교급별 지도 마커 색
# 원칙: 서로 구분되어야 할 범주는 색상환에서 멀리 떨어진 색으로.
# (기존 중학교 남색 #3b5bdb 이 병설 보라와 혼동되어 교체)
KIND_COLOR = {
    "초등학교": "#339af0",   # 파랑
    "중학교": "#2f9e44",     # 초록
    "고등학교": "#f76707",   # 주황
    "특수학교": "#e03131",   # 빨강
    "기타": "#868e96",       # 회색
}
MIXED_COLOR = "#9c36b5"      # 병설(학교급 혼합) 전용 자주색


# ══════════════════════════════════════════════════════════
# [역할 ②] 데이터 로딩 + 정제 (v2와 동일 로직)
# ══════════════════════════════════════════════════════════
@st.cache_data
def load_data() -> pd.DataFrame:
    df = pd.read_csv("gyeongbuk_schools.csv", encoding="utf-8-sig")

    # 시군 추출: 주소 두 번째 단어 ("경상북도 구미시 ..." → "구미시")
    df["시군"] = df["ORG_RDNMA"].astype(str).str.split().str[1]

    # 지원청명 축약
    df["지원청"] = (
        df["JU_ORG_NM"]
        .str.replace("경상북도교육청", "본청(도교육청)", regex=False)
        .str.replace("경상북도", "", regex=False)
        .str.replace("교육지원청", "", regex=False)
    )

    # 학교급 단순화
    main_kinds = ["초등학교", "중학교", "고등학교", "특수학교"]
    df["학교급"] = df["SCHUL_KND_SC_NM"].where(
        df["SCHUL_KND_SC_NM"].isin(main_kinds), "기타"
    )

    # 개교예정 표시 (설립일이 미래)
    fond = pd.to_datetime(df["FOND_YMD"], format="%Y%m%d", errors="coerce")
    df["개교예정"] = fond > pd.Timestamp.today()

    # 홈페이지 껍데기 값 제거
    junk = df["HMPG_ADRES"].astype(str).str.strip().isin(["http://", "https://"])
    df.loc[junk, "HMPG_ADRES"] = pd.NA

    return df


# ══════════════════════════════════════════════════════════
# [역할 ③] 위치표준데이터 로딩 + 좌표 병합
#   - 나이스 API에는 좌표가 없으므로 외부 공공데이터와 결합
#   - 파일이 없어도 앱 전체가 죽지 않도록 None 반환 (방어)
# ══════════════════════════════════════════════════════════
def read_csv_any_encoding(path: str) -> pd.DataFrame:
    """
    공공데이터 CSV는 기관마다 인코딩이 제각각(cp949 vs utf-8).
    원리: 후보 인코딩을 순서대로 시도하고, 깨지면(UnicodeDecodeError) 다음 후보로.
    (지난 교원배치 프로젝트에서 배운 자동 감지 패턴 재사용)
    """
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, "지원 인코딩 없음")


def norm_addr(s: pd.Series) -> pd.Series:
    """
    주소 정규화: 공백을 전부 제거해 표기 차이를 흡수.
    원리: '경상북도 경주시 금성로 209' vs '경상북도 경주시  금성로 209'처럼
    띄어쓰기만 다른 주소를 같은 문자열로 만들어 매칭 성공률을 높인다.
    괄호 이하(동·층 정보 등)도 잘라낸다.
    """
    return (s.astype(str)
             .str.split("(").str[0]     # 괄호 이후 부가정보 제거
             .str.replace(" ", "", regex=False)
             .str.strip())


@st.cache_data
def load_geo() -> pd.DataFrame | None:
    """위치표준데이터에서 (학교명, 학교급, 주소키, 위도, 경도)를 추출. 파일 없으면 None."""
    try:
        geo = read_csv_any_encoding(GEO_CSV)
    except FileNotFoundError:
        return None

    # 방어: 컬럼명 부분일치로 찾음 (표기 변형 대비)
    lat_col = next((c for c in geo.columns if "위도" in c), None)
    lng_col = next((c for c in geo.columns if "경도" in c), None)
    name_col = next((c for c in geo.columns if "학교명" in c), None)
    kind_col = next((c for c in geo.columns if "학교급" in c), None)
    addr_col = next((c for c in geo.columns if "도로명주소" in c), None)
    if not (lat_col and lng_col and name_col):
        return None

    # 전국 데이터 → 경북만 필터
    sido_col = next((c for c in geo.columns if "시도교육청" in c and "코드" not in c), None)
    if sido_col:
        geo = geo[geo[sido_col].astype(str).str.contains("경상북도", na=False)]

    keep = [name_col, lat_col, lng_col] + [c for c in (kind_col, addr_col) if c]
    out = geo[keep].copy()
    rename = {name_col: "학교명", lat_col: "위도", lng_col: "경도"}
    if kind_col: rename[kind_col] = "geo학교급"
    if addr_col: rename[addr_col] = "geo주소"
    out = out.rename(columns=rename)

    out["위도"] = pd.to_numeric(out["위도"], errors="coerce")
    out["경도"] = pd.to_numeric(out["경도"], errors="coerce")
    out = out.dropna(subset=["위도", "경도"])

    # 2차 매칭용 주소키 생성
    if "geo주소" in out.columns:
        out["주소키"] = norm_addr(out["geo주소"])
        # 시군 파생: 동명이교 구분용 ("경상북도 상주시 ..." → "상주시")
        out["시군"] = out["geo주소"].astype(str).str.split().str[1]

    # 동명 학교 중복 제거: 반드시 (학교명+시군) 기준!
    # 학교명만으로 제거하면 옥산초(경주/상주) 같은 동명이교 12쌍에서
    # 한쪽 좌표가 소멸 → 남은 좌표가 두 학교 모두에 잘못 붙는다 (v4.0 버그)
    dedup_keys = ["학교명", "시군"] if "시군" in out.columns else ["학교명"]
    out = out.drop_duplicates(subset=dedup_keys, keep="first")
    return out


def match_coords(view: pd.DataFrame, geo: pd.DataFrame) -> pd.DataFrame:
    """
    2단계 좌표 매칭.
      1차: 학교명 일치
      2차: 1차 실패분을 (주소키 + 학교급) 일치로 재시도
           → 개명 학교 구제 (예: 안강전자고→경북모빌리티고, 경주공업고→한국반도체마이스터고)
             위치데이터가 옛 이름을 갖고 있어도 주소·학교급이 같으면 같은 학교로 판단
    원리: 병설 학교는 '같은 주소에 학교급이 다른 학교들'이므로,
          주소만으로 매칭하면 엉뚱한 학교 좌표가 붙을 수 있다.
          그래서 반드시 학교급까지 함께 맞춰야 안전하다.
    """
    # ── 1차: 학교명 + 시군 ──
    # 학교명만 쓰면 동명이교(옥산초 경주/상주 등 12쌍)에 엉뚱한 좌표가 붙는다.
    # 시군까지 맞추면 같은 이름이라도 다른 지역이면 매칭되지 않는다.
    if "시군" in geo.columns:
        merged = view.merge(
            geo[["학교명", "시군", "위도", "경도"]],
            left_on=["SCHUL_NM", "시군"], right_on=["학교명", "시군"], how="left",
        ).drop(columns=["학교명"])
    else:
        # 방어: 위치데이터에 주소가 없어 시군 파생이 불가한 경우 이름만으로
        merged = view.merge(
            geo[["학교명", "위도", "경도"]],
            left_on="SCHUL_NM", right_on="학교명", how="left",
        ).drop(columns=["학교명"])

    # ── 2차: 주소키 + 학교급 (1차 실패분만) ──
    if "주소키" in geo.columns and "geo학교급" in geo.columns:
        miss = merged["위도"].isna()
        if miss.any():
            merged.loc[miss, "주소키"] = norm_addr(merged.loc[miss, "ORG_RDNMA"])
            # 주소키+학교급이 모두 같은 행끼리 결합 (suffix로 1차 결과와 충돌 방지)
            # drop_duplicates 필수: 동지중·동지여중처럼 '같은 주소 + 같은 학교급'인
            # 병설이 존재하면 1:2 매칭으로 행이 불어나 길이가 어긋난다.
            # 같은 지점이라 좌표는 동일하므로 하나만 남겨도 결과는 같다.
            geo2 = (geo.dropna(subset=["주소키"])
                       .drop_duplicates(subset=["주소키", "geo학교급"])
                    )[["주소키", "geo학교급", "위도", "경도"]]
            retry = merged.loc[miss].merge(
                geo2,
                left_on=["주소키", "학교급"], right_on=["주소키", "geo학교급"],
                how="left", suffixes=("", "_2차"),
            )
            # 2차에서 찾은 좌표를 원본 위치에 채워넣기 (인덱스 기준 대입)
            merged.loc[miss, "위도"] = retry["위도_2차"].values
            merged.loc[miss, "경도"] = retry["경도_2차"].values
            merged = merged.drop(columns=["주소키"], errors="ignore")

    return merged


# ══════════════════════════════════════════════════════════
# [역할 ③-2] 학교알리미 학생수 병합
#   - 알리미 SCHUL_CODE는 나이스 코드와 다른 체계 → 코드 병합 불가
#   - 대신 3중 키(학교명+학교급+시군)로 병합
#     (검증 결과: 경북에 동명이교 10쌍 존재 → 시군까지 맞춰야 안전)
# ══════════════════════════════════════════════════════════
# 명칭 불일치 별칭 사전: 알리미 표기 → 나이스 표기
# (검증에서 발견된 유일한 명칭 차이. 새로 발견되면 여기에 추가)
ALIMI_ALIAS = {
    "청송여자종합고등학교": "청송여자고등학교",
}


@st.cache_data
def load_alimi() -> pd.DataFrame | None:
    """알리미 학교현황(62)에서 병합 키 3종 + 학생수·학급수를 추출. 파일 없으면 None."""
    try:
        al = read_csv_any_encoding(ALIMI_CSV)
    except FileNotFoundError:
        return None
    need = {"SCHUL_NM", "_학교급", "_시군구", "학생수", "학급수"}
    if not need <= set(al.columns):
        return None   # 정제 컬럼이 없는 옛 수집본이면 사용하지 않음 (방어)

    al = al.copy()
    # 별칭 적용: 알리미 학교명을 나이스 표기로 통일
    al["SCHUL_NM"] = al["SCHUL_NM"].replace(ALIMI_ALIAS)
    # 시군 정규화: '포항시남구/포항시북구' → '포항시' (나이스의 시군 단위와 통일)
    al["시군"] = (al["_시군구"].str.replace("남구", "", regex=False)
                              .str.replace("북구", "", regex=False))
    keep = ["SCHUL_NM", "_학교급", "시군", "학생수", "학급수"]
    if "특수학급학생수" in al.columns:
        keep.append("특수학급학생수")
    return al[keep]


def attach_students(df: pd.DataFrame, al: pd.DataFrame) -> pd.DataFrame:
    """나이스 데이터에 알리미 학생수를 3중 키로 병합 (실패해도 원본 유지)."""
    return df.merge(
        al,
        left_on=["SCHUL_NM", "학교급", "시군"],
        right_on=["SCHUL_NM", "_학교급", "시군"],
        how="left",           # 알리미에 없는 학교(특수·기타·개교예정)도 유지
    ).drop(columns=["_학교급"], errors="ignore")


# ══════════════════════════════════════════════════════════
# [역할 ④] 나이스 실시간 API (급식·학사일정)
#   - ttl=3600: 같은 조회는 1시간 동안 캐시 재사용 → 일일 트래픽 절약
#   - 실패 시 None 반환하고 화면에서 안내 (앱이 죽지 않게)
# ══════════════════════════════════════════════════════════
def _call_neis(service: str, extra_params: dict) -> list | None:
    """나이스 API 공통 호출부. 정상이면 row 리스트, 데이터 없으면 [], 실패면 None."""
    params = {
        "KEY": API_KEY,
        "Type": "json",
        "pIndex": 1,
        "pSize": 100,
        "ATPT_OFCDC_SC_CODE": OFFICE_CODE,
        **extra_params,   # ** : 딕셔너리 풀어넣기 (extra_params 항목을 병합)
    }
    try:
        res = requests.get(f"https://open.neis.go.kr/hub/{service}", params=params, timeout=10)
        res.raise_for_status()
        data = res.json()
    except requests.RequestException:
        return None   # 네트워크 오류

    if service in data:
        return data[service][1]["row"]
    # INFO-200 = 해당 데이터 없음 → 오류가 아니라 '빈 결과'
    if data.get("RESULT", {}).get("CODE") == "INFO-200":
        return []
    return None       # 인증키 오류 등


@st.cache_data(ttl=3600)
def fetch_meal(school_code: str, ymd: str) -> list | None:
    """급식식단: mealServiceDietInfo (특정 날짜)"""
    return _call_neis("mealServiceDietInfo",
                      {"SD_SCHUL_CODE": school_code, "MLSV_YMD": ymd})


@st.cache_data(ttl=3600)
def fetch_schedule(school_code: str, from_ymd: str, to_ymd: str) -> list | None:
    """학사일정: SchoolSchedule (기간 조회)"""
    return _call_neis("SchoolSchedule",
                      {"SD_SCHUL_CODE": school_code,
                       "AA_FROM_YMD": from_ymd, "AA_TO_YMD": to_ymd})


def clean_menu(raw: str) -> str:
    """
    급식 메뉴 원문 정리.
    원문 예: "발아현미밥<br/>미역국 (5.6.9.)<br/>..."
    - <br/> → 줄바꿈
    - (5.6.9.) 같은 알레르기 번호 → 정규식으로 제거
      정규식 원리: 괄호 안에 숫자와 점([0-9.])만 반복되는 패턴을 찾아 삭제
      (백슬래시+괄호 = "진짜 괄호 문자"라는 뜻. 괄호는 정규식 예약기호라 이스케이프 필요)
    """
    text = raw.replace("<br/>", "\n").replace("<br>", "\n")
    text = re.sub(r"\s*\([0-9.\s]+\)", "", text)
    return text.strip()


# ══════════════════════════════════════════════════════════
# [역할 ⑤] 사이드바 - 필터 (v2와 동일)
# ══════════════════════════════════════════════════════════
df = load_data()
geo = load_geo()
alimi = load_alimi()
has_students = alimi is not None
if has_students:
    df = attach_students(df, alimi)   # 학생수·학급수 컬럼이 df에 추가됨

st.sidebar.title("🏫 경북 학교 현황")
st.sidebar.caption(f"코드 버전: {VERSION}")

view_mode = st.sidebar.radio(
    "보기 기준",
    ["지역 기준 (시군)", "관할 기준 (교육지원청)"],
    help="고등학교·특수학교는 본청 직할이라 '관할 기준'에서는 본청(도교육청)에 묶여 나옵니다.",
)
group_col = "시군" if view_mode.startswith("지역") else "지원청"

options = ["전체"] + df[group_col].value_counts().index.tolist()
selected = st.sidebar.selectbox(f"{group_col} 선택", options)

include_planned = st.sidebar.checkbox("개교 예정 학교 포함", value=True)

# 소규모학교 필터 (학생수 데이터가 있을 때만 노출)
only_small = False
if has_students:
    only_small = st.sidebar.checkbox(
        f"소규모학교만 보기 (전교생 {SMALL_MAX}명 이하)", value=False)

view = df if selected == "전체" else df[df[group_col] == selected]
if not include_planned:
    view = view[~view["개교예정"]]
if only_small:
    view = view[view["학생수"] <= SMALL_MAX]   # NaN(학생수 미상)은 자동 제외됨

# ══════════════════════════════════════════════════════════
# [역할 ⑥] 요약 카드 (KPI)
# ══════════════════════════════════════════════════════════
st.title(f"경상북도 학교 현황 — {selected}")

c1, c2, c3, c4 = st.columns(4)
c1.metric("전체 학교", f"{len(view):,}개교")
c2.metric("초등학교", f"{(view['학교급'] == '초등학교').sum():,}개교")
c3.metric("중학교", f"{(view['학교급'] == '중학교').sum():,}개교")
c4.metric("고등학교", f"{(view['학교급'] == '고등학교').sum():,}개교")

# ── 학생수 KPI (알리미 데이터가 있을 때만 2번째 줄로 표시) ──
if has_students and view["학생수"].notna().any():
    total_std = int(view["학생수"].sum())        # NaN은 sum에서 자동 제외
    total_cls = int(view["학급수"].sum())
    n_small = int((view["학생수"] <= SMALL_MAX).sum())
    n_known = int(view["학생수"].notna().sum())  # 학생수를 아는 학교 수(비율의 분모)
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("총 학생수", f"{total_std:,}명")
    d2.metric("총 학급수", f"{total_cls:,}학급")
    # 학급당 학생수 = 총 학생 ÷ 총 학급 (0 나눗셈 방어)
    d3.metric("학급당 학생수", f"{total_std / total_cls:.1f}명" if total_cls else "—")
    d4.metric(f"소규모학교({SMALL_MAX}명↓)",
              f"{n_small}개교", delta=f"{n_small / n_known * 100:.1f}%",
              delta_color="off")   # delta를 증감이 아닌 비율 표시로 사용
    st.caption("※ 학생수는 학교알리미 공시(학교 현황) 기준 · 특수·각종학교 등 미공시 학교는 집계에서 제외")

# ══════════════════════════════════════════════════════════
# [역할 ⑦] 탭 구조
#   st.tabs 원리: 4개의 '화면 구역'을 만들고, with 블록 안의
#   코드가 해당 탭에만 그려진다. 탭 전환은 브라우저에서 처리(재실행 없음)
# ══════════════════════════════════════════════════════════
tab_chart, tab_map, tab_school, tab_table = st.tabs(
    ["📊 현황", "🗺 지도", "🍽 학교 상세 (급식·일정)", "📋 학교 목록"]
)

# ──────────────────────────────────────────────
# 탭 1: 현황 차트 (v2와 동일)
# ──────────────────────────────────────────────
with tab_chart:
    left, right = st.columns(2)

    with left:
        st.subheader(f"{group_col}별 현황")
        kind_order = {"학교급": ["초등학교", "중학교", "고등학교", "특수학교", "기타"]}
        c_tab1, c_tab2 = st.tabs(["학교 수", "학생 수"])
        with c_tab1:
            counts = df.groupby([group_col, "학교급"]).size().reset_index(name="학교수")
            fig = px.bar(counts, x=group_col, y="학교수", color="학교급",
                         category_orders=kind_order)
            fig.update_layout(xaxis={"categoryorder": "total descending"})
            st.plotly_chart(fig, width="stretch")
        with c_tab2:
            if has_students:
                # 같은 groupby 3단 패턴, 집계만 size()→학생수 sum()으로 교체
                std = (df.dropna(subset=["학생수"])
                         .groupby([group_col, "학교급"])["학생수"]
                         .sum().reset_index(name="학생수"))
                fig2 = px.bar(std, x=group_col, y="학생수", color="학교급",
                              category_orders=kind_order)
                fig2.update_layout(xaxis={"categoryorder": "total descending"})
                st.plotly_chart(fig2, width="stretch")
            else:
                st.info(f"학생수 표시에는 `{ALIMI_CSV}` 파일이 필요합니다. "
                        "(alimi_api_test.py 실행으로 생성)")

    with right:
        st.subheader("설립 구분 · 공학 구분")
        t1, t2 = st.tabs(["설립 구분", "남녀공학 구분"])
        with t1:
            fond_cnt = view["FOND_SC_NM"].value_counts().reset_index()
            fond_cnt.columns = ["설립구분", "학교수"]
            st.plotly_chart(px.pie(fond_cnt, names="설립구분", values="학교수", hole=0.4),
                            width="stretch")
        with t2:
            coedu = view["COEDU_SC_NM"].value_counts().reset_index()
            coedu.columns = ["구분", "학교수"]
            st.plotly_chart(px.pie(coedu, names="구분", values="학교수", hole=0.4),
                            width="stretch")

# ──────────────────────────────────────────────
# 탭 2: folium 지도
# ──────────────────────────────────────────────
with tab_map:
    if geo is None:
        # 위치 파일이 없어도 앱은 계속 동작 (방어적 설계)
        st.info(
            f"지도 표시에는 좌표 데이터가 필요합니다.\n\n"
            f"1. 공공데이터포털(data.go.kr)에서 **'전국초중등학교위치표준데이터'** 검색 → CSV 다운로드\n"
            f"2. 파일명을 **`{GEO_CSV}`** 로 바꿔 dashboard.py와 같은 폴더에 저장\n"
            f"3. 좌측 상단 메뉴 ⋮ → **Clear cache** 후 Rerun"
        )
    else:
        # 2단계 매칭: ①학교명 → ②주소+학교급 (개명 학교 구제)
        merged = match_coords(view, geo)
        mapped = merged.dropna(subset=["위도", "경도"])
        n_miss = len(merged) - len(mapped)

        st.caption(
            f"좌표 매칭 {len(mapped)}/{len(merged)}개교"
            + (f" · 미매칭 {n_miss}개교 — 특수·각종·방송통신학교는 위치표준데이터에 "
               f"미포함이며, 신설교는 데이터 갱신 전입니다" if n_miss else "")
        )

        if len(mapped) == 0:
            st.warning("현재 필터에서 좌표가 매칭된 학교가 없습니다.")
        else:
            m = folium.Map(
                location=[mapped["위도"].mean(), mapped["경도"].mean()],
                zoom_start=10 if selected != "전체" else 8,
                tiles="cartodbpositron",
            )

            # ── 병설 처리: 같은 좌표의 학교들을 하나의 마커로 묶기 ──
            # groupby((위도,경도)) 원리: 좌표가 완전히 같은 행들이 한 그룹이 됨
            def radius_by_students(n) -> float:
                """
                학생수 → 마커 반지름 변환.
                원리: 원의 '면적'이 학생수에 비례해야 시각적으로 정직하다.
                면적 ∝ 반지름² 이므로 반지름 ∝ √학생수 (제곱근 스케일).
                학생수를 반지름에 그대로 쓰면 큰 학교가 과장되어 보인다.
                """
                if pd.isna(n) or n <= 0:
                    return 5.0                     # 학생수 미상: 기본 크기
                return min(4 + (n ** 0.5) / 3, 18)  # 4~18 사이로 제한

            for (lat, lng), grp in mapped.groupby(["위도", "경도"]):
                kinds = grp["학교급"].unique()
                std_sum = grp["학생수"].sum() if has_students else pd.NA
                std_txt = (f" · {int(std_sum):,}명"
                           if has_students and pd.notna(std_sum) and std_sum > 0 else "")

                if len(grp) == 1:
                    color = KIND_COLOR.get(kinds[0], "#868e96")
                    tooltip = grp.iloc[0]["SCHUL_NM"] + std_txt
                else:
                    color = KIND_COLOR.get(kinds[0], "#868e96") if len(kinds) == 1 else MIXED_COLOR
                    tooltip = " · ".join(grp["SCHUL_NM"]) + f" (병설 {len(grp)}개교){std_txt}"

                radius = radius_by_students(std_sum) if has_students else (6 if len(grp) == 1 else 8)

                # 팝업 HTML: 그룹 내 모든 학교를 나열 (+학생수·학급수)
                def school_line(r):
                    extra = ""
                    if has_students and pd.notna(getattr(r, "학생수", None)):
                        extra = f" · {int(r.학생수):,}명/{int(r.학급수)}학급"
                    return (f"<b>{r.SCHUL_NM}</b> "
                            f"<small>({r.학교급} · {r.FOND_SC_NM}{extra})</small><br>")

                lines = "".join(school_line(r) for r in grp.itertuples())
                popup_html = lines + f"<small>{grp.iloc[0]['ORG_RDNMA']}</small>"

                folium.CircleMarker(
                    location=[lat, lng],
                    radius=radius,
                    color=color,
                    fill=True, fill_opacity=0.85,
                    popup=folium.Popup(popup_html, max_width=280),
                    tooltip=tooltip,
                ).add_to(m)

            # returned_objects=[]: 지도 조작(확대 등)이 스크립트 재실행을
            # 일으키지 않도록 차단 → 체감 속도 향상
            st_folium(m, height=520, width="100%", returned_objects=[])

            # 범례: HTML로 각 점에 실제 마커 색을 입힘
            # 원리: st.caption의 ●는 전부 같은 글자색이라 범례 구실을 못 함
            # → <span style="color:...">●</span> 으로 점마다 색 지정
            # unsafe_allow_html=True: 마크다운 안에서 HTML 태그 허용 옵션
            n_shared = int(mapped.duplicated(subset=["위도", "경도"], keep=False).sum())
            legend = "&nbsp;&nbsp;".join(
                f'<span style="color:{c}">●</span> {k}' for k, c in KIND_COLOR.items()
            )
            legend += f'&nbsp;&nbsp;<span style="color:{MIXED_COLOR}">●</span> 병설(학교급 혼합)'
            if has_students:
                legend += "&nbsp;&nbsp;|&nbsp;&nbsp;원 크기 = 학생수 (면적 비례)"
            if n_shared:
                legend += f"&nbsp;&nbsp;|&nbsp;&nbsp;현재 화면 병설 학교 {n_shared}개교"
            st.markdown(
                f'<small style="color:#888">{legend}</small>',
                unsafe_allow_html=True,
            )

            # ── 미매칭 학교 안내 ──
            # 설계 원칙: 데이터에 빠진 것이 있으면 숨기지 않고
            # '무엇이 왜 빠졌는지'를 화면이 스스로 설명하게 한다
            # st.expander: 접혀 있다가 클릭하면 펼쳐지는 영역
            # → 평소엔 지도를 가리지 않고, 궁금할 때만 열어봄
            if n_miss > 0:
                missing = merged[merged["위도"].isna()].copy()

                def miss_reason(row) -> str:
                    """미매칭 사유 자동 분류 (데이터 점검에서 확인한 3가지 패턴)"""
                    if row["개교예정"]:
                        return "개교 예정 (설립일 미래)"
                    if row["학교급"] in ("특수학교", "기타"):
                        # 위치표준데이터는 초·중·고만 수록 → 특수·각종·방송통신 등은 원천 미제공
                        return "위치표준데이터 미수록 (초·중·고만 제공)"
                    return "신설·개명 추정 (위치데이터 갱신 전)"

                # axis=1: 행 단위로 함수 적용 (각 행이 row로 전달됨)
                missing["미매칭 사유"] = missing.apply(miss_reason, axis=1)

                with st.expander(f"📍 지도에 표시되지 않은 학교 {n_miss}개교 (사유 보기)"):
                    miss_table = (
                        missing[["SCHUL_NM", "SCHUL_KND_SC_NM", "시군", "미매칭 사유"]]
                        .rename(columns={"SCHUL_NM": "학교명",
                                         "SCHUL_KND_SC_NM": "학교급(상세)"})
                        .sort_values(["미매칭 사유", "학교명"])
                    )
                    st.dataframe(miss_table, width="stretch", hide_index=True)
                    st.caption(
                        "특수·각종·방송통신학교는 공공데이터포털 위치표준데이터가 "
                        "초·중·고만 수록하여 좌표가 제공되지 않습니다. "
                        "신설·개명 학교는 위치데이터 차기 갱신 시 자동 반영됩니다."
                    )

# ──────────────────────────────────────────────
# 탭 3: 학교 상세 - 급식·학사일정 (실시간 API)
# ──────────────────────────────────────────────
with tab_school:
    if not API_KEY:
        st.warning("코드 상단 API_KEY에 나이스 인증키를 입력하면 이 탭이 활성화됩니다.")
    else:
        # 학교코드가 비어있는 3개교(개교예정·각종학교)는 조회 불가 → 제외
        selectable = view[view["SD_SCHUL_CODE"].astype(str).str.strip() != ""]
        school_name = st.selectbox(
            "학교 선택", selectable["SCHUL_NM"].sort_values().tolist()
        )
        # .iloc[0]: 필터 결과(1행짜리 DataFrame)에서 첫 행을 꺼냄
        school = selectable[selectable["SCHUL_NM"] == school_name].iloc[0]
        code = str(school["SD_SCHUL_CODE"]).strip()

        col_meal, col_sched = st.columns(2)

        # ── 급식 ──
        with col_meal:
            st.subheader("🍽 급식 식단")
            picked = st.date_input("날짜", dt.date.today())
            ymd = picked.strftime("%Y%m%d")   # date 객체 → "20260712" 문자열

            meals = fetch_meal(code, ymd)
            if meals is None:
                st.error("API 호출 실패 (인증키 또는 네트워크 확인)")
            elif len(meals) == 0:
                st.info("해당 날짜의 급식 정보가 없습니다. (주말·방학·미등록)")
            else:
                for meal in meals:   # 조식/중식/석식이 각각 한 행으로 옴
                    st.markdown(f"**{meal['MMEAL_SC_NM']}** · {meal.get('CAL_INFO', '')}")
                    st.text(clean_menu(meal["DDISH_NM"]))

        # ── 학사일정 ──
        with col_sched:
            st.subheader("📅 학사일정 (오늘부터 30일)")
            today = dt.date.today()
            events = fetch_schedule(
                code,
                today.strftime("%Y%m%d"),
                (today + dt.timedelta(days=30)).strftime("%Y%m%d"),
            )
            if events is None:
                st.error("API 호출 실패 (인증키 또는 네트워크 확인)")
            elif len(events) == 0:
                st.info("향후 30일간 등록된 학사일정이 없습니다.")
            else:
                ev = pd.DataFrame(events)[["AA_YMD", "EVENT_NM"]]
                ev.columns = ["날짜", "행사명"]
                # 20260715 → 2026-07-15 형태로 보기 좋게
                ev["날짜"] = pd.to_datetime(ev["날짜"], format="%Y%m%d").dt.strftime("%m/%d (%a)")
                st.dataframe(ev, width="stretch", hide_index=True)

# ──────────────────────────────────────────────
# 탭 4: 학교 목록 + 다운로드 (v2와 동일 + 방어 코드)
# ──────────────────────────────────────────────
with tab_table:
    st.subheader(f"학교 목록 ({len(view)}개교)")

    show_cols = {
        "SCHUL_NM": "학교명",
        "학교급": "학교급",
        "시군": "시군",
        "지원청": "관할",
        "학생수": "학생수",
        "학급수": "학급수",
        "FOND_SC_NM": "설립",
        "COEDU_SC_NM": "공학구분",
        "HS_SC_NM": "고교유형",
        "ORG_RDNMA": "주소",
        "ORG_TELNO": "전화번호",
        "HMPG_ADRES": "홈페이지",
        "개교예정": "개교예정",
    }
    show_cols = {k: v for k, v in show_cols.items() if k in view.columns}
    table = view[list(show_cols)].rename(columns=show_cols)

    st.dataframe(
        table, width="stretch", hide_index=True,
        column_config={
            "홈페이지": st.column_config.LinkColumn("홈페이지"),
            "개교예정": st.column_config.CheckboxColumn("개교예정", disabled=True),
        },
    )
    st.download_button(
        "📥 현재 목록 CSV 다운로드",
        table.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"학교목록_{selected}.csv",
        mime="text/csv",
    )
