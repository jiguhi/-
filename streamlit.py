"""
네이버 쇼핑검색광고 통합 관리 (Streamlit)

세 개 기능을 하나의 UI(탭)로 통합:
  1) 카테고리별 광고그룹 생성 & 상품 등록 - CSV 업로드 -> 카테고리별 광고그룹 생성 -> SHOPPING_PRODUCT_AD 소재 일괄 등록
  2) 홍보문구 등록 (그룹 단위) - PROMOTION 확장소재
  3) 부가정보 확장소재 등록 (소재 단위) - SHOPPING_EXTRA 확장소재

실행:
    pip install -r requirements_streamlit.txt
    streamlit run 확장소재_streamlit.py

1번 탭의 "실행" 버튼은 실제로 광고그룹을 생성하고 소재를 등록합니다(라이브 API 호출).
2/3번 탭은 "미리보기"(읽기 전용)를 먼저 돌려보고, 확인 체크박스를 켠 뒤 "실행" 버튼을 눌러야
실제로 네이버에 반영됩니다.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://api.searchad.naver.com"

SETTING_FILE = "settings.json"
MAX_ADS_PER_GROUP = 1000
BATCH_SIZE = 20  # 한 번에 등록할 상품 수. 처음엔 20 권장, 안정적이면 50으로 증가 가능
RESULT_FILE = "progress_result.csv"
MAX_GROUP_NAME_LENGTH = 30
REQUEST_DELAY = 0.3


# =========================
# 설정 저장/불러오기 (1번 탭용)
# =========================
def load_settings():
    if os.path.exists(SETTING_FILE):
        try:
            with open(SETTING_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return {"api_key": "", "secret_key": "", "customer_id": "", "campaign_id": ""}


def save_settings(api_key, secret_key, customer_id, campaign_id):
    data = {
        "api_key": api_key,
        "secret_key": secret_key,
        "customer_id": customer_id,
        "campaign_id": campaign_id,
    }
    with open(SETTING_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# =========================
# 인증 / API 요청 (공통)
# =========================
def generate_signature(timestamp, method, uri, secret_key):
    message = f"{timestamp}.{method}.{uri}"
    hash_value = hmac.new(bytes(secret_key, "utf-8"), bytes(message, "utf-8"), hashlib.sha256)
    return base64.b64encode(hash_value.digest()).decode()


def get_header(api_key, secret_key, customer_id, method, uri):
    timestamp = str(round(time.time() * 1000))
    signature = generate_signature(timestamp, method, uri, secret_key)
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": timestamp,
        "X-API-KEY": api_key,
        "X-Customer": str(customer_id),
        "X-Signature": signature,
    }


def api_request(api_key, secret_key, customer_id, method, uri, params=None, json_data=None):
    url = BASE_URL + uri
    sign_uri = uri.split("?")[0]
    wait_list = [10, 20, 40, 60, 90, 120]

    last_res = None
    for wait in wait_list:
        headers = get_header(api_key, secret_key, customer_id, method, sign_uri)
        res = requests.request(
            method=method, url=url, headers=headers, params=params, json=json_data, timeout=30
        )
        last_res = res

        if res.status_code == 400 and "1014" in res.text:
            time.sleep(wait)
            continue

        return res

    return last_res


# =========================
# 상품 / 카테고리 처리 (1번 탭용)
# =========================
def get_col(row, keyword):
    keyword_clean = keyword.replace(" ", "")
    for col in row.index:
        col_clean = str(col).replace(" ", "")
        if keyword_clean in col_clean:
            return row.get(col, "")
    return ""


CATEGORY_LEVEL_KEYS = {
    "소분류": ["대분류", "중분류", "소분류", "세분류"],
    "중분류": ["대분류", "중분류"],
}


def make_category_name(row, category_level="소분류"):
    keys = CATEGORY_LEVEL_KEYS.get(category_level, CATEGORY_LEVEL_KEYS["소분류"])
    cats = [row.get(k, "") for k in keys]
    cats = [str(c).strip() for c in cats if str(c).strip() and str(c).strip().lower() != "nan"]
    return " > ".join(cats) if cats else "카테고리없음"


def clean_group_name(name):
    """광고그룹명을 정리하고 최대 30자로 제한합니다."""
    name = str(name)
    name = name.replace(">", "-")
    name = name.replace("/", "-")
    name = re.sub(r"[^가-힣a-zA-Z0-9\s_-]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"\s*-\s*", "-", name)

    if not name:
        name = "카테고리없음"

    return name[:MAX_GROUP_NAME_LENGTH]


def make_group_name_with_suffix(base_name, number):
    """'기본 그룹명 2'처럼 번호를 붙이더라도 전체 길이가 30자를 넘지 않도록 처리합니다."""
    suffix = f" {number}"
    available_length = MAX_GROUP_NAME_LENGTH - len(suffix)
    return f"{base_name[:available_length]}{suffix}"


def build_category_group_name_map(grouped):
    """
    카테고리별 광고그룹명을 생성합니다.
    그룹명은 최대 30자로 제한하고, 동일한 이름이 만들어지면 2, 3 등의 번호를 붙입니다.
    """
    category_group_name_map = {}
    used_group_names = set()

    for category_name in grouped.keys():
        base_group_name = clean_group_name(category_name)
        group_name = base_group_name

        number = 2
        while group_name in used_group_names:
            group_name = make_group_name_with_suffix(base_group_name, number)
            number += 1

        category_group_name_map[category_name] = group_name
        used_group_names.add(group_name)

    return category_group_name_map


def load_products_from_df(df, category_level="소분류"):
    if "판매상태" in df.columns:
        df = df[df["판매상태"].astype(str).str.contains("판매", na=False)]
    if "전시상태" in df.columns:
        df = df[df["전시상태"].astype(str).str.contains("전시", na=False)]

    products = []
    for _, row in df.iterrows():
        shopping_product_no = str(get_col(row, "네이버쇼핑상품번호(스마트스토어)")).strip()
        if not shopping_product_no or shopping_product_no.lower() == "nan":
            shopping_product_no = str(get_col(row, "상품번호(스마트스토어)")).strip()

        product_no = str(get_col(row, "상품번호(스마트스토어)")).strip()
        product_name = str(row.get("상품명", "")).strip()
        category_name = make_category_name(row, category_level)

        if not shopping_product_no or shopping_product_no.lower() == "nan":
            continue

        products.append({
            "product_no": product_no,
            "shopping_product_no": shopping_product_no,
            "product_name": product_name,
            "category_name": category_name,
            "group_name": clean_group_name(category_name),
        })

    return products


def group_products(products):
    grouped = defaultdict(list)
    for product in products:
        grouped[product["category_name"]].append(product)
    return grouped


# =========================
# 네이버 광고그룹 / 소재 (1번 탭용)
# =========================
def get_adgroups(api_key, secret_key, customer_id):
    uri = "/ncc/adgroups"
    res = api_request(api_key, secret_key, customer_id, "GET", uri)
    if not res.ok:
        raise Exception(f"광고그룹 조회 실패: {res.status_code} / {res.text}")
    return res.json()


def get_adgroup_map_by_campaign(api_key, secret_key, customer_id, campaign_id):
    groups = get_adgroups(api_key, secret_key, customer_id)
    group_map = {}
    for g in groups:
        if g.get("nccCampaignId") == campaign_id:
            group_map[g.get("name")] = {
                "nccAdgroupId": g.get("nccAdgroupId"),
                "pcChannelId": g.get("pcChannelId"),
                "mobileChannelId": g.get("mobileChannelId"),
                "pcChannelKey": g.get("pcChannelKey"),
                "mobileChannelKey": g.get("mobileChannelKey"),
                "adgroupType": g.get("adgroupType"),
            }
    return group_map


def get_existing_ads_with_count(api_key, secret_key, customer_id, adgroup_id):
    uri = "/ncc/ads"
    params = {"nccAdgroupId": adgroup_id}
    res = api_request(api_key, secret_key, customer_id, "GET", uri, params=params)

    if not res.ok:
        return set(), 0

    existing_refs = set()
    ads = res.json()
    for ad in ads:
        ref = str(ad.get("referenceKey", "")).strip()
        if ref:
            existing_refs.add(ref)

    return existing_refs, len(ads)


def get_existing_ads(api_key, secret_key, customer_id, adgroup_id):
    uri = "/ncc/ads"
    params = {"nccAdgroupId": adgroup_id}
    res = api_request(api_key, secret_key, customer_id, "GET", uri, params=params)

    if not res.ok:
        return set()

    existing_refs = set()
    for ad in res.json():
        ref = str(ad.get("referenceKey", "")).strip()
        if ref:
            existing_refs.add(ref)

    return existing_refs


def get_existing_refs_by_campaign(api_key, secret_key, customer_id, campaign_id, log_box=None):
    """
    캠페인 안의 모든 광고그룹 소재를 조회해서 이미 등록된 referenceKey(상품번호)를
    전체 기준으로 수집합니다. 이 목록에 있는 상품은 다른 광고그룹에도 추가 등록하지 않습니다.
    """
    group_map = get_adgroup_map_by_campaign(api_key, secret_key, customer_id, campaign_id)

    campaign_existing_refs = set()
    campaign_ad_count = 0

    if log_box:
        log_box.write("[캠페인 전체 등록 상품 조회 시작]")

    scan_progress = st.progress(0)
    total_groups = len(group_map)

    for idx, (group_name, group_info) in enumerate(group_map.items(), start=1):
        adgroup_id = group_info.get("nccAdgroupId")
        refs, ad_count = get_existing_ads_with_count(api_key, secret_key, customer_id, adgroup_id)

        campaign_existing_refs.update(refs)
        campaign_ad_count += ad_count

        if log_box:
            log_box.write(
                f"[기존 소재 조회] {idx}/{total_groups} "
                f"{group_name} / 소재 {ad_count:,}개 / "
                f"누적 상품 {len(campaign_existing_refs):,}개"
            )

        if total_groups > 0:
            scan_progress.progress(idx / total_groups)

        time.sleep(0.3)

    if log_box:
        log_box.write(
            f"[캠페인 전체 기존 등록 상품 조회 완료] "
            f"총 소재 {campaign_ad_count:,}개 / "
            f"중복 제외 기준 상품 {len(campaign_existing_refs):,}개"
        )

    return campaign_existing_refs


def get_channel_ids_from_same_campaign(api_key, secret_key, customer_id, campaign_id):
    groups = get_adgroups(api_key, secret_key, customer_id)

    for g in groups:
        if g.get("nccCampaignId") == campaign_id and g.get("adgroupType") == "SHOPPING":
            pc = g.get("pcChannelId")
            mobile = g.get("mobileChannelId")
            if pc and mobile:
                return {
                    "base_group_name": g.get("name"),
                    "pcChannelId": pc,
                    "mobileChannelId": mobile,
                    "pcChannelKey": g.get("pcChannelKey"),
                    "mobileChannelKey": g.get("mobileChannelKey"),
                }

    raise Exception("같은 캠페인 안에 SHOPPING 기준 광고그룹이 없습니다. 광고센터에서 쇼핑검색 광고그룹 1개를 먼저 수동 생성하세요.")


def create_adgroup(
    api_key, secret_key, customer_id, campaign_id, group_name,
    pc_channel_id, mobile_channel_id, group_bid_amt, contents_bid_amt,
):
    uri = "/ncc/adgroups"
    payload = {
        "name": group_name,
        "nccCampaignId": campaign_id,
        "adgroupType": "SHOPPING",
        "bidAmt": int(group_bid_amt),
        "contentsNetworkBidAmt": int(contents_bid_amt),
        "contentsNetworkBidWeight": 100,
        "mobileNetworkBidWeight": 100,
        "pcNetworkBidWeight": 100,
        "dailyBudget": 0,
        "userLock": False,
        "pcChannelId": pc_channel_id,
        "mobileChannelId": mobile_channel_id,
    }
    return api_request(api_key, secret_key, customer_id, "POST", uri, json_data=payload)


def make_next_group_name(base_group_name, group_map):
    idx = 1
    while True:
        new_name = make_group_name_with_suffix(base_group_name, idx)
        if new_name not in group_map:
            return new_name
        idx += 1


def find_available_group(api_key, secret_key, customer_id, base_group_name, group_map):
    candidate_names = [base_group_name]

    idx = 1
    while True:
        name = make_group_name_with_suffix(base_group_name, idx)
        if name in group_map:
            candidate_names.append(name)
            idx += 1
        else:
            break

    last_existing_name = candidate_names[-1]
    last_group_info = group_map.get(last_existing_name)
    last_adgroup_id = last_group_info.get("nccAdgroupId")

    existing_refs, ad_count = get_existing_ads_with_count(
        api_key, secret_key, customer_id, last_adgroup_id
    )

    if ad_count < MAX_ADS_PER_GROUP:
        return {
            "group_name": last_existing_name,
            "adgroup_id": last_adgroup_id,
            "existing_refs": existing_refs,
            "ad_count": ad_count,
            "need_create": False,
            "new_group_name": None,
        }

    new_group_name = make_next_group_name(base_group_name, group_map)
    return {
        "group_name": new_group_name,
        "adgroup_id": None,
        "existing_refs": set(),
        "ad_count": 0,
        "need_create": True,
        "new_group_name": new_group_name,
    }


def create_missing_adgroups(
    api_key, secret_key, customer_id, campaign_id, grouped,
    category_group_name_map, group_bid_amt, contents_bid_amt, log_box,
):
    channels = get_channel_ids_from_same_campaign(api_key, secret_key, customer_id, campaign_id)

    log_box.write(f"기준 광고그룹: {channels['base_group_name']}")
    log_box.write(f"채널: {channels.get('pcChannelKey')}")

    group_map = get_adgroup_map_by_campaign(api_key, secret_key, customer_id, campaign_id)

    created_rows = []
    progress = st.progress(0)
    total = len(grouped)

    for idx, category_name in enumerate(grouped.keys(), start=1):
        group_name = category_group_name_map[category_name]

        if group_name in group_map:
            adgroup_id = group_map[group_name]["nccAdgroupId"]
            log_box.write(f"[기존 그룹 사용] {group_name} / {adgroup_id}")
            created_rows.append({
                "category_name": category_name,
                "group_name": group_name,
                "adgroup_id": adgroup_id,
                "group_status": "기존",
            })
            progress.progress(idx / total)
            continue

        res = create_adgroup(
            api_key=api_key, secret_key=secret_key, customer_id=customer_id,
            campaign_id=campaign_id, group_name=group_name,
            pc_channel_id=channels["pcChannelId"], mobile_channel_id=channels["mobileChannelId"],
            group_bid_amt=group_bid_amt, contents_bid_amt=contents_bid_amt,
        )

        if res.ok:
            data = res.json()
            adgroup_id = data.get("nccAdgroupId")
            group_map[group_name] = {
                "nccAdgroupId": adgroup_id,
                "pcChannelId": channels["pcChannelId"],
                "mobileChannelId": channels["mobileChannelId"],
                "adgroupType": "SHOPPING",
            }
            log_box.write(f"[그룹 생성 완료] {group_name} / {adgroup_id}")
            created_rows.append({
                "category_name": category_name,
                "group_name": group_name,
                "adgroup_id": adgroup_id,
                "group_status": "생성",
            })
        else:
            log_box.write(f"[그룹 생성 실패] {group_name} / {res.status_code} / {res.text}")
            created_rows.append({
                "category_name": category_name,
                "group_name": group_name,
                "adgroup_id": "",
                "group_status": f"실패: {res.status_code} {res.text}",
            })

        progress.progress(idx / total)
        time.sleep(1)

    return group_map, created_rows


def create_shopping_product_ad(api_key, secret_key, customer_id, adgroup_id, shopping_product_no, group_bid_amt):
    uri = "/ncc/ads?isList=true"
    payload = [{
        "nccAdgroupId": adgroup_id,
        "type": "SHOPPING_PRODUCT_AD",
        "referenceKey": str(shopping_product_no),
        "ad": {},
        "adAttr": {"bidAmt": int(group_bid_amt), "useGroupBidAmt": True},
        "userLock": False,
    }]
    return api_request(api_key, secret_key, customer_id, "POST", uri, json_data=payload)


def create_shopping_product_ads_batch(api_key, secret_key, customer_id, adgroup_id, shopping_product_nos, group_bid_amt):
    """SHOPPING_PRODUCT_AD 소재를 여러 개 한 번에 등록합니다. /ncc/ads?isList=true 리스트 payload 사용."""
    uri = "/ncc/ads?isList=true"
    payload = []
    for shopping_no in shopping_product_nos:
        payload.append({
            "nccAdgroupId": adgroup_id,
            "type": "SHOPPING_PRODUCT_AD",
            "referenceKey": str(shopping_no).strip(),
            "ad": {},
            "adAttr": {"bidAmt": int(group_bid_amt), "useGroupBidAmt": True},
            "userLock": False,
        })
    return api_request(api_key, secret_key, customer_id, "POST", uri, json_data=payload)


def load_progress():
    if os.path.exists(RESULT_FILE):
        try:
            return pd.read_csv(RESULT_FILE, dtype=str, encoding="utf-8-sig")
        except Exception:
            pass
    return pd.DataFrame()


def save_progress_row(row):
    df_row = pd.DataFrame([row])
    file_exists = os.path.exists(RESULT_FILE)
    df_row.to_csv(RESULT_FILE, mode="a", header=not file_exists, index=False, encoding="utf-8-sig")


def register_products_to_adgroups(
    api_key, secret_key, customer_id, campaign_id, grouped,
    category_group_name_map, group_map, group_bid_amt, contents_bid_amt,
    log_box, batch_size=20,
):
    result_rows = []
    progress_df = load_progress()

    done_refs = set()
    if not progress_df.empty and "shopping_product_no" in progress_df.columns:
        done_refs = set(
            progress_df[
                progress_df["status"].isin(["등록완료", "중복스킵", "캠페인내이미등록"])
            ]["shopping_product_no"].astype(str).str.strip()
        )

    success_count = 0
    fail_count = 0
    skip_count = 0

    all_products_count = sum(len(v) for v in grouped.values())
    current = 0
    progress = st.progress(0)

    channels = get_channel_ids_from_same_campaign(api_key, secret_key, customer_id, campaign_id)

    campaign_existing_refs = get_existing_refs_by_campaign(
        api_key, secret_key, customer_id, campaign_id, log_box=log_box
    )

    batch_size = int(batch_size) if batch_size else 20
    batch_size = max(1, batch_size)

    def add_result(row):
        result_rows.append(row)
        save_progress_row(row)

    for category_name, products in grouped.items():
        base_group_name = category_group_name_map[category_name]
        group_info = group_map.get(base_group_name)

        if not group_info:
            log_box.write(f"[스킵] 광고그룹 없음: {base_group_name}")
            for product in products:
                skip_count += 1
                current += 1
                add_result({
                    "category_name": category_name,
                    "group_name": base_group_name,
                    "adgroup_id": "",
                    "shopping_product_no": product["shopping_product_no"],
                    "product_name": product["product_name"],
                    "status": "스킵",
                    "message": "광고그룹 없음",
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                progress.progress(current / all_products_count)
            continue

        available = find_available_group(api_key, secret_key, customer_id, base_group_name, group_map)

        active_group_name = available["group_name"]
        adgroup_id = available["adgroup_id"]
        existing_refs = available["existing_refs"]
        ad_count = available["ad_count"]

        if available["need_create"]:
            log_box.write(f"[기존 확장그룹도 1000개 도달] 새 광고그룹 생성: {active_group_name}")

            res_group = create_adgroup(
                api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                campaign_id=campaign_id, group_name=active_group_name,
                pc_channel_id=channels["pcChannelId"], mobile_channel_id=channels["mobileChannelId"],
                group_bid_amt=group_bid_amt, contents_bid_amt=contents_bid_amt,
            )

            if res_group.ok:
                new_data = res_group.json()
                adgroup_id = new_data.get("nccAdgroupId")
                group_map[active_group_name] = {
                    "nccAdgroupId": adgroup_id,
                    "pcChannelId": channels["pcChannelId"],
                    "mobileChannelId": channels["mobileChannelId"],
                    "adgroupType": "SHOPPING",
                }
                existing_refs = set()
                ad_count = 0
                log_box.write(f"[새 그룹 생성 완료] {active_group_name} / {adgroup_id}")
            else:
                log_box.write(f"[새 그룹 생성 실패] {active_group_name} / {res_group.status_code} / {res_group.text}")
                for product in products:
                    fail_count += 1
                    current += 1
                    add_result({
                        "category_name": category_name,
                        "group_name": active_group_name,
                        "adgroup_id": "",
                        "shopping_product_no": product["shopping_product_no"],
                        "product_name": product["product_name"],
                        "status": "등록실패",
                        "message": f"새 광고그룹 생성 실패: {res_group.status_code} / {res_group.text}",
                        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    progress.progress(current / all_products_count)
                continue

        log_box.write(f"상품 등록 대상 선별 시작: {active_group_name} / {adgroup_id} / 현재 소재수 {ad_count}")

        register_targets = []
        for product in products:
            current += 1
            shopping_no = str(product["shopping_product_no"]).strip()
            product_name = product["product_name"]

            if shopping_no in done_refs:
                skip_count += 1
                log_box.write(f"[이미 처리됨 스킵] {shopping_no} / {product_name}")
                progress.progress(current / all_products_count)
                continue

            if not shopping_no or shopping_no.lower() == "nan":
                skip_count += 1
                add_result({
                    "category_name": category_name,
                    "group_name": active_group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "스킵",
                    "message": "상품번호 없음",
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                progress.progress(current / all_products_count)
                continue

            if shopping_no in campaign_existing_refs:
                skip_count += 1
                done_refs.add(shopping_no)
                log_box.write(f"[캠페인 내 이미 등록된 상품 제외] {shopping_no} / {product_name}")
                add_result({
                    "category_name": category_name,
                    "group_name": active_group_name,
                    "adgroup_id": adgroup_id,
                    "shopping_product_no": shopping_no,
                    "product_name": product_name,
                    "status": "캠페인내이미등록",
                    "message": "같은 캠페인 내 다른 광고그룹에 이미 등록된 상품이므로 등록하지 않음",
                    "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                progress.progress(current / all_products_count)
                continue

            register_targets.append(product)
            progress.progress(current / all_products_count)

        if not register_targets:
            log_box.write(f"[등록 대상 없음] {active_group_name}")
            continue

        log_box.write(
            f"[배치 등록 시작] {active_group_name} / 등록 대상 {len(register_targets):,}개 / "
            f"배치 {batch_size}개 단위"
        )

        target_idx = 0
        while target_idx < len(register_targets):
            remaining_capacity = MAX_ADS_PER_GROUP - ad_count

            if remaining_capacity <= 0:
                active_group_name = make_next_group_name(base_group_name, group_map)
                log_box.write(f"[소재 1000개 도달] 새 광고그룹 생성: {active_group_name}")

                res_group = create_adgroup(
                    api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                    campaign_id=campaign_id, group_name=active_group_name,
                    pc_channel_id=channels["pcChannelId"], mobile_channel_id=channels["mobileChannelId"],
                    group_bid_amt=group_bid_amt, contents_bid_amt=contents_bid_amt,
                )

                if res_group.ok:
                    new_data = res_group.json()
                    adgroup_id = new_data.get("nccAdgroupId")
                    group_map[active_group_name] = {
                        "nccAdgroupId": adgroup_id,
                        "pcChannelId": channels["pcChannelId"],
                        "mobileChannelId": channels["mobileChannelId"],
                        "adgroupType": "SHOPPING",
                    }
                    existing_refs = set()
                    ad_count = 0
                    remaining_capacity = MAX_ADS_PER_GROUP
                    log_box.write(f"[새 그룹 생성 완료] {active_group_name} / {adgroup_id}")
                else:
                    fail_count += 1
                    if res_group.status_code == 400 and "1014" in res_group.text:
                        log_box.write("[호출 제한 초과] API 제한이 풀리지 않아 실행을 중단합니다. 잠시 후 다시 실행해주세요.")
                        st.stop()

                    log_box.write(f"[새 그룹 생성 실패] {active_group_name} / {res_group.status_code} / {res_group.text}")

                    for product in register_targets[target_idx:]:
                        add_result({
                            "category_name": category_name,
                            "group_name": active_group_name,
                            "adgroup_id": "",
                            "shopping_product_no": product["shopping_product_no"],
                            "product_name": product["product_name"],
                            "status": "등록실패",
                            "message": f"새 광고그룹 생성 실패: {res_group.status_code} / {res_group.text}",
                            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        fail_count += 1
                    break

            batch_count = min(batch_size, remaining_capacity, len(register_targets) - target_idx)
            batch = register_targets[target_idx:target_idx + batch_count]
            batch_nos = [str(p["shopping_product_no"]).strip() for p in batch]

            log_box.write(
                f"[배치 등록] {active_group_name} / "
                f"{target_idx + 1}~{target_idx + len(batch)} / {len(register_targets)}개"
            )

            res = create_shopping_product_ads_batch(
                api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                adgroup_id=adgroup_id, shopping_product_nos=batch_nos, group_bid_amt=group_bid_amt,
            )

            if res.ok:
                for product in batch:
                    shopping_no = str(product["shopping_product_no"]).strip()
                    product_name = product["product_name"]

                    success_count += 1
                    existing_refs.add(shopping_no)
                    campaign_existing_refs.add(shopping_no)
                    done_refs.add(shopping_no)
                    ad_count += 1

                    add_result({
                        "category_name": category_name,
                        "group_name": active_group_name,
                        "adgroup_id": adgroup_id,
                        "shopping_product_no": shopping_no,
                        "product_name": product_name,
                        "status": "등록완료",
                        "message": "배치등록 성공",
                        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })

                target_idx += len(batch)
                time.sleep(0.1)
                continue

            if res.status_code == 400 and "1014" in res.text:
                log_box.write("[호출 제한 초과] API 제한이 풀리지 않아 작업을 종료합니다.")
                st.stop()

            log_box.write(f"[배치 등록 실패 → 개별 재시도] {res.status_code} / {res.text}")

            for product in batch:
                shopping_no = str(product["shopping_product_no"]).strip()
                product_name = product["product_name"]

                single_res = create_shopping_product_ad(
                    api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                    adgroup_id=adgroup_id, shopping_product_no=shopping_no, group_bid_amt=group_bid_amt,
                )

                if single_res.ok:
                    success_count += 1
                    existing_refs.add(shopping_no)
                    campaign_existing_refs.add(shopping_no)
                    done_refs.add(shopping_no)
                    ad_count += 1

                    log_box.write(f"[개별 등록 완료] {shopping_no} / {product_name}")
                    add_result({
                        "category_name": category_name,
                        "group_name": active_group_name,
                        "adgroup_id": adgroup_id,
                        "shopping_product_no": shopping_no,
                        "product_name": product_name,
                        "status": "등록완료",
                        "message": "배치 실패 후 개별등록 성공",
                        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                else:
                    fail_count += 1
                    if single_res.status_code == 400 and "1014" in single_res.text:
                        log_box.write("[호출 제한 초과] API 제한이 풀리지 않아 작업을 종료합니다.")
                        st.stop()

                    log_box.write(
                        f"[개별 등록 실패] {shopping_no} / {product_name} / "
                        f"{single_res.status_code} / {single_res.text}"
                    )
                    add_result({
                        "category_name": category_name,
                        "group_name": active_group_name,
                        "adgroup_id": adgroup_id,
                        "shopping_product_no": shopping_no,
                        "product_name": product_name,
                        "status": "등록실패",
                        "message": f"{single_res.status_code} / {single_res.text}",
                        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })

                time.sleep(0.1)

            target_idx += len(batch)

    summary = {"success_count": success_count, "fail_count": fail_count, "skip_count": skip_count}
    return result_rows, summary


# =========================
# 홍보문구(PROMOTION, 그룹 단위) - 2번 탭용
# =========================
def get_campaigns(api_key, secret_key, customer_id):
    res = api_request(api_key, secret_key, customer_id, "GET", "/ncc/campaigns")
    if not res.ok:
        raise Exception(f"캠페인 조회 실패: {res.status_code} / {res.text}")
    return res.json()


def get_adgroups_by_campaign(api_key, secret_key, customer_id, campaign_id):
    res = api_request(
        api_key, secret_key, customer_id, "GET", "/ncc/adgroups",
        params={"nccCampaignId": campaign_id},
    )
    if not res.ok:
        raise Exception(f"광고그룹 조회 실패: {res.status_code} / {res.text}")
    return res.json()


def get_ads_in_group(api_key, secret_key, customer_id, adgroup_id):
    res = api_request(
        api_key, secret_key, customer_id, "GET", "/ncc/ads",
        params={"nccAdgroupId": adgroup_id},
    )
    if not res.ok:
        return []
    return res.json()


def get_ad_extensions_by_owner(api_key, secret_key, customer_id, owner_id):
    res = api_request(
        api_key, secret_key, customer_id, "GET", "/ncc/ad-extensions",
        params={"ownerId": owner_id},
    )
    if not res.ok:
        return []
    return res.json()


def find_existing_promotion(api_key, secret_key, customer_id, adgroup_id):
    for ext in get_ad_extensions_by_owner(api_key, secret_key, customer_id, adgroup_id):
        if ext.get("type") == "PROMOTION":
            return ext
    return None


def create_promotion(api_key, secret_key, customer_id, adgroup_id, text1, text2):
    payload = {
        "ownerId": adgroup_id,
        "type": "PROMOTION",
        "userLock": False,
        "headline": text1,
        "description": text2,
    }
    return api_request(api_key, secret_key, customer_id, "POST", "/ncc/ad-extensions", json_data=payload)


def update_promotion(api_key, secret_key, customer_id, ext_id, adgroup_id, text1, text2):
    payload = {
        "nccAdExtensionId": ext_id,
        "ownerId": adgroup_id,
        "type": "PROMOTION",
        "userLock": False,
        "headline": text1,
        "description": text2,
    }
    return api_request(api_key, secret_key, customer_id, "PUT", f"/ncc/ad-extensions/{ext_id}", json_data=payload)


def run_promotion(api_key, secret_key, customer_id, campaigns, text1, text2, dry_run, progress_cb=None):
    adgroup_list = []
    for c in campaigns:
        for ag in get_adgroups_by_campaign(api_key, secret_key, customer_id, c["nccCampaignId"]):
            adgroup_list.append((c["name"], ag))

    total = len(adgroup_list)
    rows = []

    for i, (campaign_name, ag) in enumerate(adgroup_list, 1):
        adgroup_id = ag["nccAdgroupId"]
        adgroup_name = ag.get("name")
        existing = find_existing_promotion(api_key, secret_key, customer_id, adgroup_id)

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
                    res = create_promotion(api_key, secret_key, customer_id, adgroup_id, text1, text2)
                else:
                    res = update_promotion(
                        api_key, secret_key, customer_id,
                        existing["nccAdExtensionId"], adgroup_id, text1, text2,
                    )

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
# 부가정보(SHOPPING_EXTRA, 소재 단위) - 3번 탭용
# =========================
def is_already_applied(api_key, secret_key, customer_id, ad_id):
    exts = get_ad_extensions_by_owner(api_key, secret_key, customer_id, ad_id)
    return any(e.get("type") == "SHOPPING_EXTRA" for e in exts)


def apply_shopping_product_info(api_key, secret_key, customer_id, ad_id):
    payload = {"ownerId": ad_id, "type": "SHOPPING_EXTRA"}
    return api_request(api_key, secret_key, customer_id, "POST", "/ncc/ad-extensions", json_data=payload)


def run_shopping_extra(api_key, secret_key, customer_id, campaigns, dry_run, progress_cb=None):
    ad_list = []
    for c in campaigns:
        for ag in get_adgroups_by_campaign(api_key, secret_key, customer_id, c["nccCampaignId"]):
            adgroup_id = ag["nccAdgroupId"]
            adgroup_name = ag.get("name")
            for ad in get_ads_in_group(api_key, secret_key, customer_id, adgroup_id):
                ad_list.append((c["name"], adgroup_name, ad))

    total = len(ad_list)
    rows = []

    for i, (campaign_name, adgroup_name, ad) in enumerate(ad_list, 1):
        ad_id = ad.get("nccAdId")
        ref = ad.get("referenceKey", "")
        already = is_already_applied(api_key, secret_key, customer_id, ad_id)

        if already:
            action, status, message = "-", "이미적용", ""
        else:
            action = "등록"
            if dry_run:
                status, message = "미리보기", ""
            else:
                res = apply_shopping_product_info(api_key, secret_key, customer_id, ad_id)
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


def make_progress(progress_bar, log_box):
    log_lines = []

    def cb(i, total, msg):
        progress_bar.progress(i / total if total else 1.0)
        log_lines.append(msg)
        log_box.text("\n".join(log_lines[-15:]))

    return cb


# =========================
# Streamlit UI
# =========================
st.set_page_config(page_title="그라펜 쇼핑검색광고 통합 관리", layout="wide")
st.title("그라펜 쇼핑검색광고 통합 관리")

st.warning("API Key/Secret Key는 화면 입력값으로만 사용하는 것을 권장합니다.")

with st.sidebar:
    st.header("네이버 검색광고 API 계정")
    settings = load_settings()
    api_key = st.text_input("API Key", value=settings.get("api_key", ""), type="password")
    secret_key = st.text_input("Secret Key", value=settings.get("secret_key", ""), type="password")
    customer_id = st.text_input("Customer ID", value=settings.get("customer_id", ""))
    st.caption("대행사 키라 Customer ID만 바꾸면 다른 광고주 계정에도 그대로 쓸 수 있습니다.")

tab_group, tab_promo, tab_extra = st.tabs([
    "카테고리별 광고그룹 생성 & 상품 등록",
    "홍보문구 등록 (광고그룹 단위)",
    "부가정보 확장소재 등록 (소재 단위)",
])

# ---------- Tab 1: 카테고리별 광고그룹 생성 & 상품 등록 ----------
with tab_group:
    st.caption("VERSION 2026-08-19 카테고리그룹기준 추가")

    col_a, col_b = st.columns(2)
    with col_a:
        campaign_id = st.text_input("Campaign ID", value=settings.get("campaign_id", ""), key="campaign_id_1")
        category_level = st.radio(
            "광고그룹을 어떤 카테고리 기준으로 만들지 선택하세요.",
            options=["소분류", "중분류"],
            index=0,
            horizontal=True,
            help="소분류: 대분류>중분류>소분류>세분류 기준(기존 방식) / 중분류: 대분류>중분류 기준",
            key="category_level_1",
        )
    with col_b:
        group_bid_amt = st.number_input("광고그룹 기본 입찰가", min_value=70, value=200, step=10, key="group_bid_1")
        contents_bid_amt = st.number_input("콘텐츠 네트워크 입찰가", min_value=70, value=200, step=10, key="contents_bid_1")
        batch_size = st.number_input("배치 등록 개수", min_value=1, max_value=100, value=BATCH_SIZE, step=5, key="batch_size_1")

    col_c, col_d = st.columns(2)
    with col_c:
        run_create_groups = st.checkbox("카테고리별 광고그룹 생성", value=True, key="run_create_groups_1")
    with col_d:
        run_register_products = st.checkbox("각 광고그룹에 상품 등록", value=True, key="run_register_products_1")

    uploaded_file = st.file_uploader("상품 CSV 파일 업로드", type=["csv"], key="uploaded_file_1")

    df = None
    products = []
    grouped = {}
    category_group_name_map = {}

    if uploaded_file:
        df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
        products = load_products_from_df(df, category_level)
        grouped = group_products(products)
        category_group_name_map = build_category_group_name_map(grouped)

        st.info(f"현재 선택된 카테고리 그룹 기준: **{category_level}**")

        col1, col2, col3 = st.columns(3)
        col1.metric("CSV 전체 행", f"{len(df):,}")
        col2.metric("등록 대상 상품", f"{len(products):,}")
        col3.metric("생성 대상 카테고리", f"{len(grouped):,}")

        st.subheader("CSV 컬럼")
        st.write(list(df.columns))

        preview_df = pd.DataFrame([
            {
                "카테고리": category,
                "광고그룹명": category_group_name_map[category],
                "그룹명길이": len(category_group_name_map[category]),
                "상품수": len(items),
            }
            for category, items in grouped.items()
        ])

        st.subheader(f"카테고리({category_level})별 광고그룹 미리보기")
        st.dataframe(preview_df, use_container_width=True)

        with st.expander("상품 미리보기"):
            st.dataframe(pd.DataFrame(products).head(100), use_container_width=True)

    col_test, col_run = st.columns([1, 2])

    with col_test:
        if st.button("API 연결 테스트", key="api_test_1"):
            if not all([api_key, secret_key, customer_id]):
                st.error("API Key, Secret Key, Customer ID를 입력해주세요.")
            else:
                try:
                    res = api_request(api_key, secret_key, customer_id, "GET", "/ncc/adgroups")
                    if res.ok:
                        st.success("API 연결 성공")
                        st.write(f"광고그룹 수: {len(res.json())}")
                    else:
                        st.error(f"API 연결 실패: {res.status_code} / {res.text}")
                except Exception as e:
                    st.error(str(e))

    with col_run:
        run_btn = st.button("광고그룹 생성 및 상품 등록 실행", type="primary", key="run_btn_1")

    if run_btn:
        save_settings(api_key, secret_key, customer_id, campaign_id)

        if not uploaded_file:
            st.error("CSV 파일을 업로드해주세요.")
        elif not all([api_key, secret_key, customer_id, campaign_id]):
            st.error("API Key, Secret Key, Customer ID, Campaign ID를 모두 입력해주세요.")
        elif not run_create_groups and not run_register_products:
            st.error("실행 옵션을 최소 1개 이상 선택해주세요.")
        else:
            log_box = st.empty()
            log_text = []

            class LogBox:
                def write(self, msg):
                    log_text.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")
                    log_box.code("\n".join(log_text[-80:]))

            logger = LogBox()

            try:
                group_result_rows = []
                register_result_rows = []

                if run_create_groups:
                    st.subheader(f"1. 광고그룹 생성 ({category_level} 기준)")
                    group_map, group_result_rows = create_missing_adgroups(
                        api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                        campaign_id=campaign_id, grouped=grouped,
                        category_group_name_map=category_group_name_map,
                        group_bid_amt=group_bid_amt, contents_bid_amt=contents_bid_amt,
                        log_box=logger,
                    )
                else:
                    group_map = get_adgroup_map_by_campaign(
                        api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                        campaign_id=campaign_id,
                    )

                if run_register_products:
                    st.subheader("2. 상품 소재 등록")
                    register_result_rows, summary = register_products_to_adgroups(
                        api_key=api_key, secret_key=secret_key, customer_id=customer_id,
                        campaign_id=campaign_id, grouped=grouped,
                        category_group_name_map=category_group_name_map, group_map=group_map,
                        group_bid_amt=group_bid_amt, contents_bid_amt=contents_bid_amt,
                        log_box=logger, batch_size=batch_size,
                    )

                    st.success("실행 완료")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("등록 성공", summary["success_count"])
                    c2.metric("등록 실패", summary["fail_count"])
                    c3.metric("중복/스킵", summary["skip_count"])

                    total_count = summary["success_count"] + summary["fail_count"] + summary["skip_count"]
                    success_rate = (summary["success_count"] / total_count * 100) if total_count > 0 else 0

                    st.subheader("작업 결과 리포트")
                    report_df = pd.DataFrame([
                        {"항목": "전체 처리 상품 수", "결과": f"{total_count:,}개"},
                        {"항목": "등록 성공", "결과": f"{summary['success_count']:,}개"},
                        {"항목": "등록 실패", "결과": f"{summary['fail_count']:,}개"},
                        {"항목": "중복/스킵", "결과": f"{summary['skip_count']:,}개"},
                        {"항목": "성공률", "결과": f"{success_rate:.1f}%"},
                        {"항목": "작업 완료 시간", "결과": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    ])

                    st.dataframe(report_df, use_container_width=True)
                    st.download_button(
                        "작업 결과 리포트 CSV 다운로드",
                        report_df.to_csv(index=False, encoding="utf-8-sig"),
                        file_name="naver_shopping_work_summary.csv",
                        mime="text/csv",
                        key="download_report_1",
                    )

                    if group_result_rows:
                        st.subheader("광고그룹 생성 결과")
                        group_df = pd.DataFrame(group_result_rows)
                        st.dataframe(group_df, use_container_width=True)
                        st.download_button(
                            "광고그룹 생성 결과 CSV 다운로드",
                            group_df.to_csv(index=False, encoding="utf-8-sig"),
                            file_name="naver_adgroup_create_result.csv",
                            mime="text/csv",
                            key="download_group_1",
                        )

                    if register_result_rows:
                        st.subheader("상품 등록 결과")
                        result_df = pd.DataFrame(register_result_rows)
                        st.dataframe(result_df, use_container_width=True)
                        st.download_button(
                            "상품 등록 결과 CSV 다운로드",
                            result_df.to_csv(index=False, encoding="utf-8-sig"),
                            file_name="naver_product_register_result.csv",
                            mime="text/csv",
                            key="download_result_1",
                        )

            except Exception as e:
                st.error(f"실행 중 오류 발생: {e}")

# ---------- Tab 2: 홍보문구 ----------
with tab_promo:
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
                st.session_state["campaigns_1"] = get_campaigns(api_key, secret_key, customer_id)
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
            df_promo = run_promotion(
                api_key, secret_key, customer_id, selected_campaigns_1, text1, text2,
                dry_run=True, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["preview_df_1"] = df_promo

    if "preview_df_1" in st.session_state:
        st.dataframe(st.session_state["preview_df_1"], use_container_width=True)

    confirm_1 = st.checkbox("실제로 네이버 광고그룹에 반영합니다 (라이브 API 호출)", key="confirm_1")
    if st.button("🚀 실행 (실제 등록/수정)", key="run_1", disabled=not (ready_1 and confirm_1)):
        progress_bar = st.progress(0)
        log_box = st.empty()
        with st.spinner("등록/수정 중..."):
            df_promo = run_promotion(
                api_key, secret_key, customer_id, selected_campaigns_1, text1, text2,
                dry_run=False, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["result_df_1"] = df_promo
        st.success("완료!")

    if "result_df_1" in st.session_state:
        df_promo = st.session_state["result_df_1"]
        st.dataframe(df_promo, use_container_width=True)
        st.download_button(
            "결과 CSV 다운로드",
            df_promo.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"홍보문구_등록결과_{datetime.now():%Y%m%d_%H%M%S}.csv",
            key="download_2",
        )

# ---------- Tab 3: 부가정보 확장소재 ----------
with tab_extra:
    st.subheader("부가정보(SHOPPING_EXTRA) 등록")
    st.caption(
        "쇼핑검색광고 소재(SHOPPING_PRODUCT_AD)의 부가정보(리뷰수/평점/찜수/구매수) 노출을 켭니다. "
        "이미 켜져 있는 소재는 건너뜁니다."
    )

    if st.button("캠페인 목록 불러오기", key="load_campaigns_2"):
        with st.spinner("캠페인 조회 중..."):
            try:
                st.session_state["campaigns_2"] = get_campaigns(api_key, secret_key, customer_id)
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
            df_extra = run_shopping_extra(
                api_key, secret_key, customer_id, selected_campaigns_2,
                dry_run=True, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["preview_df_2"] = df_extra

    if "preview_df_2" in st.session_state:
        st.dataframe(st.session_state["preview_df_2"], use_container_width=True)

    confirm_2 = st.checkbox("실제로 네이버 소재에 반영합니다 (라이브 API 호출)", key="confirm_2")
    if st.button("🚀 실행 (실제 등록)", key="run_2", disabled=not (ready_2 and confirm_2)):
        progress_bar = st.progress(0)
        log_box = st.empty()
        with st.spinner("등록 중..."):
            df_extra = run_shopping_extra(
                api_key, secret_key, customer_id, selected_campaigns_2,
                dry_run=False, progress_cb=make_progress(progress_bar, log_box),
            )
        st.session_state["result_df_2"] = df_extra
        st.success("완료!")

    if "result_df_2" in st.session_state:
        df_extra = st.session_state["result_df_2"]
        st.dataframe(df_extra, use_container_width=True)
        st.download_button(
            "결과 CSV 다운로드",
            df_extra.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"부가정보_등록결과_{datetime.now():%Y%m%d_%H%M%S}.csv",
            key="download_3",
        )
