"""
그라펜 - 네이버 쇼핑검색광고 확장소재 관리 (Streamlit)

두 개 스크립트를 하나의 UI로 통합:
  1) 그라펜_쇼핑_그룹_홍보문구_등록.py  -> 광고그룹 단위 홍보문구(PROMOTION) 등록/수정
  2) 그라펜_쇼핑_부가정보_확장소재_등록.py -> 소재 단위 부가정보(SHOPPING_EXTRA) 등록

실행:
    pip install streamlit
    streamlit run 그라펜_확장소재_streamlit.py

두 기능 모두 "미리보기"(읽기 전용, 실제 API 호출 없음)를 먼저 돌려보고,
확인 체크박스를 켠 뒤 "실행" 버튼을 눌러야 실제로 네이버에 반영됩니다.
"""

import base64
import hashlib
import hmac
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://api.searchad.naver.com"

# 대행사 키라 CUSTOMER_ID만 바꾸면 다른 광고주 계정에도 그대로 쓸 수 있습니다.
# 보안을 위해 기본값을 코드에 넣지 않았습니다 - 그라펜 계정 값은 그라펜_쇼핑_부가정보_확장소재_등록.py
# 상단(API_KEY / SECRET_KEY / CUSTOMER_ID = '763477')에서 그대로 복사해 사이드바에 붙여넣어 쓰세요.
REQUEST_DELAY = 0.3


# =========================
# 네이버 검색광고 API 공통
# =========================
def make_signature(secret_key, timestamp, method, uri):
    message = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def get_headers(creds, method, uri):
    api_key, secret_key, customer_id = creds
    timestamp = str(int(time.time() * 1000))
    return {
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": customer_id,
        "X-Signature": make_signature(secret_key, timestamp, method, uri),
        "Content-Type": "application/json; charset=UTF-8",
    }


def api_request(creds, method, uri, params=None, json_data=None, log_cb=None):
    sign_uri = uri.split("?")[0]
    wait_list = [10, 20, 40, 60, 90, 120]

    last_res = None
    for wait in wait_list:
        res = requests.request(
            method=method,
            url=BASE_URL + uri,
            headers=get_headers(creds, method, sign_uri),
            params=params,
            json=json_data,
            timeout=30,
        )
        last_res = res

        if res.status_code == 400 and "1014" in res.text:
            if log_cb:
                log_cb(f"[API 호출 제한] {wait}초 대기 후 재시도")
            time.sleep(wait)
            continue

        return res

    return last_res


def naver_get(creds, uri, params=None):
    res = api_request(creds, "GET", uri, params=params)
    if not res.ok:
        raise RuntimeError(f"GET {uri} 실패: {res.status_code} / {res.text}")
    return res.json()


def get_campaigns(creds):
    return naver_get(creds, "/ncc/campaigns")


def get_adgroups(creds, campaign_id):
    return naver_get(creds, "/ncc/adgroups", params={"nccCampaignId": campaign_id})


def get_ads(creds, adgroup_id):
    try:
        return naver_get(creds, "/ncc/ads", params={"nccAdgroupId": adgroup_id})
    except Exception:
        return []


def get_ad_extensions_by_owner(creds, owner_id):
    try:
        return naver_get(creds, "/ncc/ad-extensions", params={"ownerId": owner_id})
    except Exception:
        return []


# =========================
# 1) 홍보문구 (PROMOTION, 광고그룹 단위)
# =========================
def find_existing_promotion(creds, adgroup_id):
    for ext in get_ad_extensions_by_owner(creds, adgroup_id):
        if ext.get("type") == "PROMOTION":
            return ext
    return None


def create_promotion(creds, adgroup_id, text1, text2):
    payload = {
        "ownerId": adgroup_id,
        "type": "PROMOTION",
        "userLock": False,
        "headline": text1,
        "description": text2,
    }
    return api_request(creds, "POST", "/ncc/ad-extensions", json_data=payload)


def update_promotion(creds, ext_id, adgroup_id, text1, text2):
    payload = {
        "nccAdExtensionId": ext_id,
        "ownerId": adgroup_id,
        "type": "PROMOTION",
        "userLock": False,
        "headline": text1,
        "description": text2,
    }
    return api_request(creds, "PUT", f"/ncc/ad-extensions/{ext_id}", json_data=payload)


def run_promotion(creds, campaigns, text1, text2, dry_run, progress_cb=None):
    adgroup_list = []
    for c in campaigns:
        for ag in get_adgroups(creds, c["nccCampaignId"]):
            adgroup_list.append((c["name"], ag))

    total = len(adgroup_list)
    rows = []

    for i, (campaign_name, ag) in enumerate(adgroup_list, 1):
        adgroup_id = ag["nccAdgroupId"]
        adgroup_name = ag.get("name")
        existing = find_existing_promotion(creds, adgroup_id)

        if existing is None:
            action = "생성"
        elif existing.get("headline") == text1 and existing.get("description") == text2:
            action = "변경없음"
        else:
            action = "수정"

        status, message = ("미리보기", "") if dry_run else ("", "")

        if not dry_run:
            if action == "변경없음":
                status = "스킵(동일)"
            else:
                if action == "생성":
                    res = create_promotion(creds, adgroup_id, text1, text2)
                else:
                    res = update_promotion(creds, existing["nccAdExtensionId"], adgroup_id, text1, text2)

                if res is not None and res.ok:
                    status = "성공"
                else:
                    status = "실패"
                    message = f"{res.status_code} / {res.text}" if res is not None else "응답 없음"
                time.sleep(REQUEST_DELAY)

        rows.append({
            "캠페인": campaign_name,
            "광고그룹": adgroup_name,
            "광고그룹ID": adgroup_id,
            "동작": action,
            "상태": status,
            "메시지": message,
        })

        if progress_cb:
            progress_cb(i, total, f"{campaign_name} / {adgroup_name} -> {action} {status}")

    return pd.DataFrame(rows)


# =========================
# 2) 부가정보 (SHOPPING_EXTRA, 소재 단위)
# =========================
def is_already_applied(creds, ad_id):
    exts = get_ad_extensions_by_owner(creds, ad_id)
    return any(e.get("type") == "SHOPPING_EXTRA" for e in exts)


def apply_shopping_product_info(creds, ad_id):
    payload = {"ownerId": ad_id, "type": "SHOPPING_EXTRA"}
    return api_request(creds, "POST", "/ncc/ad-extensions", json_data=payload)


def run_shopping_extra(creds, campaigns, dry_run, progress_cb=None):
    ad_list = []
    for c in campaigns:
        for ag in get_adgroups(creds, c["nccCampaignId"]):
            adgroup_id = ag["nccAdgroupId"]
            adgroup_name = ag.get("name")
            for ad in get_ads(creds, adgroup_id):
                ad_list.append((c["name"], adgroup_name, ad))

    total = len(ad_list)
    rows = []

    for i, (campaign_name, adgroup_name, ad) in enumerate(ad_list, 1):
        ad_id = ad.get("nccAdId")
        ref = ad.get("referenceKey", "")
        already = is_already_applied(creds, ad_id)

        if already:
            action, status, message = "-", "이미적용", ""
        else:
            action = "등록"
            if dry_run:
                status, message = "미리보기", ""
            else:
                res = apply_shopping_product_info(creds, ad_id)
                if res is not None and res.ok:
                    status, message = "성공", ""
                else:
                    status = "실패"
                    message = f"{res.status_code} / {res.text}" if res is not None else "응답 없음"
                time.sleep(REQUEST_DELAY)

        rows.append({
            "캠페인": campaign_name,
            "광고그룹": adgroup_name,
            "소재ID": ad_id,
            "referenceKey": ref,
            "동작": action,
            "상태": status,
            "메시지": message,
        })

        if progress_cb:
            progress_cb(i, total, f"{campaign_name} / {adgroup_name} / {ad_id} -> {status}")

    return pd.DataFrame(rows)


# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="쇼핑검색광고 확장소재 관리", layout="wide")
st.title("쇼핑검색광고 확장소재 관리")

with st.sidebar:
    st.header("네이버 검색광고 API 계정")
    api_key = st.text_input("API Key", value="")
    secret_key = st.text_input("Secret Key", value="", type="password")
    customer_id = st.text_input("Customer ID", value="")
    st.caption("대행사 키라 CUSTOMER_ID만 바꾸면 다른 광고주 계정에도 그대로 쓸 수 있습니다.")

creds = (api_key, secret_key, customer_id)

if not (api_key and secret_key and customer_id):
    st.info("왼쪽 사이드바에 API Key / Secret Key / Customer ID를 먼저 입력해주세요.")
    st.stop()


def make_progress(progress_bar, log_box):
    log_lines = []

    def cb(i, total, msg):
        progress_bar.progress(i / total if total else 1.0)
        log_lines.append(msg)
        log_box.text("\n".join(log_lines[-15:]))

    return cb


tab1, tab2 = st.tabs(["홍보문구 등록 (광고그룹 단위)", "부가정보 확장소재 등록 (소재 단위)"])

# ---------- Tab 1: 홍보문구 ----------
with tab1:
    st.subheader("홍보문구(PROMOTION) 등록/수정")
    st.caption(
        "문구1(최대 10자) + 문구2(최대 30자) 세트를 광고그룹 단위로 등록합니다. "
        "이미 등록된 문구와 내용이 같으면 건너뜁니다."
    )

    col1, col2 = st.columns(2)
    with col1:
        text1 = st.text_input("문구1 (최대 10자)", key="promo_text1")
        if text1 and len(text1) > 10:
            st.warning(f"문구1이 {len(text1)}자입니다. 10자를 넘었어요.")
    with col2:
        text2 = st.text_input("문구2 (최대 30자)", key="promo_text2")
        if text2 and len(text2) > 30:
            st.warning(f"문구2가 {len(text2)}자입니다. 30자를 넘었어요.")

    if st.button("캠페인 목록 불러오기", key="load_campaigns_1"):
        with st.spinner("캠페인 조회 중..."):
            try:
                st.session_state["campaigns_1"] = get_campaigns(creds)
            except Exception as e:
                st.error(f"캠페인 조회 실패: {e}")

    campaigns_1 = st.session_state.get("campaigns_1", [])
    selected_names_1 = st.multiselect(
        "대상 캠페인 선택", [c["name"] for c in campaigns_1], key="selected_campaigns_1"
    )
    selected_campaigns_1 = [c for c in campaigns_1 if c["name"] in selected_names_1]

    ready_1 = bool(text1 and text2 and selected_campaigns_1)

    if st.button("🔍 미리보기 (실제 등록 안 함)", key="preview_1", disabled=not ready_1):
        progress_bar = st.progress(0)
        log_box = st.empty()
        with st.spinner("조회 중..."):
            df = run_promotion(
                creds, selected_campaigns_1, text1, text2,
                dry_run=True, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["preview_df_1"] = df

    if "preview_df_1" in st.session_state:
        st.dataframe(st.session_state["preview_df_1"], use_container_width=True)

    confirm_1 = st.checkbox("실제로 네이버 광고그룹에 반영합니다 (라이브 API 호출)", key="confirm_1")
    if st.button("🚀 실행 (실제 등록/수정)", key="run_1", disabled=not (ready_1 and confirm_1)):
        progress_bar = st.progress(0)
        log_box = st.empty()
        with st.spinner("등록/수정 중..."):
            df = run_promotion(
                creds, selected_campaigns_1, text1, text2,
                dry_run=False, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["result_df_1"] = df
        st.success("완료!")

    if "result_df_1" in st.session_state:
        df = st.session_state["result_df_1"]
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "결과 CSV 다운로드",
            df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"홍보문구_등록결과_{datetime.now():%Y%m%d_%H%M%S}.csv",
            key="download_1",
        )

# ---------- Tab 2: 부가정보 확장소재 ----------
with tab2:
    st.subheader("부가정보(SHOPPING_EXTRA) 등록")
    st.caption(
        "쇼핑검색광고 소재(SHOPPING_PRODUCT_AD)의 부가정보(리뷰수/평점/찜수/구매수) 노출을 켭니다. "
        "이미 켜져 있는 소재는 건너뜁니다."
    )

    if st.button("캠페인 목록 불러오기", key="load_campaigns_2"):
        with st.spinner("캠페인 조회 중..."):
            try:
                st.session_state["campaigns_2"] = get_campaigns(creds)
            except Exception as e:
                st.error(f"캠페인 조회 실패: {e}")

    campaigns_2 = st.session_state.get("campaigns_2", [])
    selected_names_2 = st.multiselect(
        "대상 캠페인 선택", [c["name"] for c in campaigns_2], key="selected_campaigns_2"
    )
    selected_campaigns_2 = [c for c in campaigns_2 if c["name"] in selected_names_2]

    ready_2 = bool(selected_campaigns_2)

    if st.button("🔍 미리보기 (실제 등록 안 함)", key="preview_2", disabled=not ready_2):
        progress_bar = st.progress(0)
        log_box = st.empty()
        with st.spinner("조회 중... (소재 수가 많으면 시간이 걸릴 수 있어요)"):
            df = run_shopping_extra(
                creds, selected_campaigns_2,
                dry_run=True, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["preview_df_2"] = df

    if "preview_df_2" in st.session_state:
        st.dataframe(st.session_state["preview_df_2"], use_container_width=True)

    confirm_2 = st.checkbox("실제로 네이버 소재에 반영합니다 (라이브 API 호출)", key="confirm_2")
    if st.button("🚀 실행 (실제 등록)", key="run_2", disabled=not (ready_2 and confirm_2)):
        progress_bar = st.progress(0)
        log_box = st.empty()
        with st.spinner("등록 중..."):
            df = run_shopping_extra(
                creds, selected_campaigns_2,
                dry_run=False, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["result_df_2"] = df
        st.success("완료!")

    if "result_df_2" in st.session_state:
        df = st.session_state["result_df_2"]
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "결과 CSV 다운로드",
            df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"부가정보_등록결과_{datetime.now():%Y%m%d_%H%M%S}.csv",
            key="download_2",
        )
