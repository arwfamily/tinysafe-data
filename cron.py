import os
import json
import hashlib
import requests
import pandas as pd
from datetime import datetime, timedelta
import time

DB_PATH = "recalls_unified.json"
KEYWORDS = ["baby", "kids", "toddler", "infant"]

def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_db(data):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_hash_id(date, company, brand):
    text = f"{date}_{company}_{brand}".lower().replace(" ", "")
    return "dt-" + hashlib.md5(text.encode("utf-8")).hexdigest()[:12]

# 베이비/유아용 제품 노이즈 필터 (농산물 제외 및 유아용 브랜드 구출)
def is_child_product(product_desc, brand_name, company_name):
    text = f"{product_desc} {brand_name} {company_name}".lower()
    drop_keywords = ["spinach", "arugula", "romaine", "lettuce", "kale", "salad", "vegetable tray", "baby bella", "bok choy", "okra", "peaches", "plums", "onions"]
    if any(dk in text for dk in drop_keywords):
        rescue_keywords = ["baby food", "puree", "pouch", "yobaby", "gerber", "infant formula", "wipe", "diaper"]
        if not any(rk in text for rk in rescue_keywords):
            return False
    return any(kw in text for kw in KEYWORDS)

# [소스 1] FDA Datatables: 네가 준 URL 기반 4개 키워드 순회 다운로드
def fetch_fda_datatables():
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    unique_records = {}
    
    for kw in KEYWORDS:
        print(f"[*] FDA Datatables 다운로드 중... 키워드: {kw}")
        url = f"https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/datatables-data?_format=xlsx&search_api_fulltext={kw}&field_regulated_product_field=All&field_terminated_recall=All"
        
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"[-] FDA Datatables 실패 ({kw}): {response.status_code}")
                continue
                
            df = pd.read_excel(response.content)
            for _, row in df.iterrows():
                date_str = str(row.get("Date", ""))
                brand = str(row.get("Brand-Names", ""))
                desc = str(row.get("Product-Description", ""))
                company = str(row.get("Company-Name", ""))
                
                if not is_child_product(desc, brand, company):
                    continue
                    
                hash_id = generate_hash_id(date_str, company, brand)
                if hash_id not in unique_records:
                    unique_records[hash_id] = {
                        "source": "FDA_Datatables",
                        "recall_id": hash_id,
                        "date": date_str,
                        "brand": brand,
                        "product_description": desc,
                        "company": company,
                        "classification": "Pending",
                        "status": "Ongoing" if str(row.get("Terminated Recall", "")) == "No" else "Terminated",
                        "is_fully_enforced": False
                    }
            time.sleep(2) # 봇 차단 회피를 위한 휴식
        except Exception as e:
            print(f"[-] FDA Datatables 에러 ({kw}): {e}")
            
    return list(unique_records.values())

# [소스 2] OpenFDA Enforcement API: 크로스체크용 정식 보고서 동시 수집
def fetch_openfda_enforcement():
    print("[*] OpenFDA 정식 Enforcement API 수집 중...")
    records = []
    # 4개 키워드를 OR 조건으로 묶어서 한 번에 요청
    search_query = 'product_description:("baby" "kids" "toddler" "infant")'
    url = f"https://api.fda.gov/food/enforcement.json?search={search_query}&limit=100"
    
    try:
        response = requests.get(url, timeout=30)
        if response.status_code != 200:
            return []
            
        results = response.json().get("results", [])
        for item in results:
            desc = item.get("product_description", "")
            brand = item.get("brand_name", "")
            company = item.get("recalling_firm", "")
            
            if not is_child_product(desc, brand, company):
                continue
                
            raw_date = item.get("recall_initiation_date", "")
            formatted_date = f"{raw_date[4:6]}/{raw_date[6:8]}/{raw_date[0:4]}" if len(raw_date) == 8 else raw_date
            
            records.append({
                "source": "OpenFDA",
                "recall_id": item.get("recall_number", ""),
                "date": formatted_date,
                "brand": brand,
                "product_description": desc,
                "company": company,
                "classification": item.get("classification", "Class I"),
                "status": item.get("status", "Ongoing"),
                "is_fully_enforced": True
            })
        return records
    except Exception as e:
        print(f"[-] OpenFDA API 에러: {e}")
        return []

# [소스 3] CPSC API: 소비재 공식 API 수집
def fetch_cpsc_api():
    print("[*] CPSC 공식 API 수집 중...")
    start_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    url = f"https://www.saferproducts.gov/RestWebServices/Recall?format=json&RecallDateStart={start_date}"
    
    try:
        response = requests.get(url, timeout=30)
        if response.status_code != 200:
            return []
            
        cpsc_data = response.json()
        records = []
        for item in cpsc_data:
            title = item.get("Title", "")
            desc = item.get("Description", "")
            full_text = f"{title} {desc}".lower()
            recall_id = str(item.get("RecallNumber", ""))
            
            if any(kw in full_text for kw in KEYWORDS):
                records.append({
                    "source": "CPSC",
                    "recall_id": recall_id,
                    "date": item.get("RecallDate", "")[:10],
                    "brand": item.get("BrandNames", [{}]).get("Name", "") if item.get("BrandNames") else "",
                    "product_description": title,
                    "company": item.get("Manufacturers", [{}]).get("Name", "") if item.get("Manufacturers") else "",
                    "classification": "Class I",
                    "status": "Ongoing",
                    "is_fully_enforced": True
                })
        return records
    except Exception as e:
        print(f"[-] CPSC API 에러: {e}")
        return []

# 4대 소스 동시 크로스체크 및 매칭(승급) 로직
def cross_check_and_merge(existing_db, new_datatables, openfda_data, new_cpsc):
    updated_db = {r["recall_id"]: r for r in existing_db}
    
    # 1. 정식 데이터셋들(OpenFDA, CPSC) 먼저 베이스 등록 및 업데이트
    for fda_ref in openfda_data:
        updated_db[fda_ref["recall_id"]] = fda_ref
    for cpsc in new_cpsc:
        updated_db[cpsc["recall_id"]] = cpsc

    # 2. 최신 Datatables를 대조하여 '승급' 혹은 '임시 유지' 결정
    for dt_record in new_datatables:
        matched_enforcement = None
        
        for ex_id, ex_rec in updated_db.items():
            if ex_rec.get("is_fully_enforced") and ex_rec["source"] == "OpenFDA":
                # 보수적 매칭: 제조사나 브랜드가 겹치는지 크로스체크
                if dt_record["company"].lower() in ex_rec["company"].lower() or dt_record["brand"].lower() in ex_rec["brand"].lower():
                    if dt_record["brand"].lower()[:5] in ex_rec["brand"].lower():
                        matched_enforcement = ex_rec
                        break
        
        if matched_enforcement:
            # 매칭 성공 시 정식 데이터에 최신 상태 동기화 및 legacy_id 저장
            print(f"[+] 승급 확인: {dt_record['brand']} -> 정식 ID {matched_enforcement['recall_id']}")
            matched_enforcement["status"] = dt_record["status"]
            matched_enforcement["legacy_id"] = dt_record["recall_id"] # 옛 임시 ID 딥링크 보존용
        else:
            # 아직 정식 보고서가 없는 진짜 최신 리콜이면 임시 레코드로 등록
            if dt_record["recall_id"] not in updated_db:
                updated_db[dt_record["recall_id"]] = dt_record

    return list(updated_db.values())

if __name__ == "__main__":
    db = load_db()
    print(f"[*] 로드된 기존 DB: {len(db)}건")
    
    dt_data = fetch_fda_datatables()
    openfda_data = fetch_openfda_enforcement()
    cpsc_data = fetch_cpsc_api()
    
    print(f"[*] 이번 배치 수집 결과 - Datatables: {len(dt_data)}건 / OpenFDA: {len(openfda_data)}건 / CPSC: {len(cpsc_data)}건")
    
    final_db = cross_check_and_merge(db, dt_data, openfda_data, cpsc_data)
    save_db(final_db)
    print(f"[*] 동시 크로스체크 업데이트 완료. 최종 DB: {len(final_db)}건")
