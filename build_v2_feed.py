#!/usr/bin/env python3
"""
TinySafe V2 — build_v2_feed.py
================================
canonical(DailyMed, percent_ww) → 진짜 미네랄 선크림 정제 → 모든 스택/순위/리콜/상황
플래그 사전계산 → 앱이 읽을 피드. 앱은 계산 안 하고 플래그만 읽음.

입력:
  --canonical  us_sunscreens.jsonl   (canonical, percent_ww/percent_basis 있음)
  --legacy     tinysafe_baby_sunscreens.json  (현 앱피드: ndc/wearability/asin 복원용)
  --recalls    recalls_unified.json
출력:
  --out        tinysafe_baby_sunscreens.json  (앱 URL 불변)

파이프라인:
  1. 소스 게이트(사실만): 미네랄 활성 + 등록 화학필터 active에 없음
  2. 정제: 비선크림 제외(기저귀/화장품/치료) + SPF 게이트 + 립/메이크업/정크 제외
  3. baby_basis 분류 (5단계)
  4. 사전계산: 17 배제 bool + 4 순위 지수(품질게이트) + 리콜 매칭 + 제형/SPF + NDC 복원
"""
import argparse, json, re, datetime
from collections import defaultdict, Counter

# ═══════════════ 토큰 사전 (전부 세션 검증됨) ═══════════════
CHEM_FILTERS=["AVOBENZONE","OXYBENZONE","OCTINOXATE","OCTYL METHOXYCINNAMATE","OCTISALATE","OCTYL SALICYLATE",
  "HOMOSALATE","OCTOCRYLENE","ENSULIZOLE","MEXORYL","MERADIMATE","PADIMATE","SULISOBENZONE","DIOXYBENZONE","CINOXATE","TROLAMINE SALICYLATE"]
BOOSTERS=["BUTYLOCTYL SALICYLATE","TRIDECYL SALICYLATE","ETHYLHEXYL METHOXYCRYLENE","METHOXYCRYLENE","ETHYL FERULATE","POLYESTER-8","POLYCRYLENE"]
FORMALDEHYDE=["DMDM HYDANTOIN","IMIDAZOLIDINYL UREA","DIAZOLIDINYL UREA","QUATERNIUM-15","BRONOPOL","SODIUM HYDROXYMETHYLGLYCINATE"]
MIMCI=["METHYLISOTHIAZOLINONE","METHYLCHLOROISOTHIAZOLINONE"]
FRAG=["FRAGRANCE","PARFUM"]
EU26=["LIMONENE","LINALOOL","CITRONELLOL","GERANIOL","CITRAL","EUGENOL","ISOEUGENOL","COUMARIN","BENZYL ALCOHOL","BENZYL SALICYLATE","BENZYL BENZOATE","BENZYL CINNAMATE","CINNAMAL","CINNAMYL ALCOHOL","FARNESOL","HEXYL CINNAMAL","AMYL CINNAMAL","AMYLCINNAMYL","HYDROXYCITRONELLAL","ANISE ALCOHOL","ANISYL","BUTYLPHENYL METHYLPROPIONAL","ISOMETHYL IONONE","METHYL 2-OCTYNOATE","EVERNIA PRUNASTRI","EVERNIA FURFURACEA","HYDROXYISOHEXYL"]
RETINOL=["RETINOL","RETINYL","RETINALDEHYDE","RETINOIC","VITAMIN A"]
PETRO=["PETROLATUM","MINERAL OIL","PARAFFINUM LIQUIDUM","PETROLEUM JELLY"]
CYCLIC=re.compile(r"CYCLOTETRASILOXANE|CYCLOPENTASILOXANE|CYCLOHEXASILOXANE|CYCLOMETHICONE")
ETHOX=re.compile(r"PEG-|PEG/|PPG-|POLYETHYLENE GLYCOL|POLYOXYETHYLENE|POLYSORBATE|OXYNOL|CETEARETH|LAURETH|STEARETH|OLETH|CETETH|TRIDECETH|TRILAURETH")
# 순위: 발림성 Zeidler 값
SV={"ISODODECANE":1400,"ISOHEXADECANE":1300,"CYCLOPENTASILOXANE":1300,"CYCLOHEXASILOXANE":1100,"CYCLOMETHICONE":1200,
  "DICAPRYLYL CARBONATE":1050,"DICAPRYLYL ETHER":1100,"C12-15 ALKYL BENZOATE":1000,"ALKYL (C12-15) BENZOATE":1000,
  "HYDROGENATED POLYISOBUTENE":1000,"COCO-CAPRYLATE":950,"PROPYLHEPTYL CAPRYLATE":1000,"CAPRYLYL METHICONE":900,
  "CAPRYLIC/CAPRIC TRIGLYCERIDE":500,"MEDIUM-CHAIN TRIGLYCERIDES":500,"DIMETHICONE":500,"ETHYLHEXYL PALMITATE":600,
  "SQUALANE":450,"JOJOBA":250,"SUNFLOWER":220,"HELIANTHUS":220,"COCONUT OIL":200,"COCOS NUCIFERA":200,"OLIVE":180,"OLEA":180,
  "SHEA":120,"BUTYROSPERMUM":120,"COCOA BUTTER":100,"THEOBROMA":100,"CASTOR":90,"RICINUS":90,"BEESWAX":40,"CERA ALBA":40,
  "CANDELILLA":40,"CARNAUBA":40,"PETROLATUM":80,"LANOLIN":100,"AVOCADO":180}
# 순위: 진정 성분
SOOTHE={"PANTHENOL":2,"ALLANTOIN":2,"BISABOLOL":2,"LEVOMENOL":2,"GLYCYRRHIZ":2,"MADECASSOSIDE":2,
  "ALOE":1.5,"AVENA":1.5,"COLLOIDAL OAT":1.5,"OAT KERNEL":1.5,"CENTELLA":1.5,"CHAMOMIL":1.5,"MATRICARIA":1.5,
  "CALENDULA":1,"GREEN TEA":1,"CAMELLIA SINENSIS":1,"NIACINAMIDE":1,"CERAMIDE":1,"HYALURON":1}


# 순위: 피부장벽 (배리어) — 세라마이드·콜레스테롤·지방산·스쿠알란
BARRIER={"CERAMIDE":3,"CHOLESTEROL":2,"SQUALANE":1.5,"LINOLEIC":1.5,"LINOLENIC":1.5,"PHYTOSPHINGOSINE":2,"BEHENIC ACID":0.5,"STEARIC ACID":0.5,"FATTY ACID":1}
# 순위: 항산화 — 나이아신아마이드·녹차·비타민C·페룰릭 (비타민E는 너무 흔해 약가중)
ANTIOX={"NIACINAMIDE":2,"CAMELLIA SINENSIS":1.5,"GREEN TEA":1.5,"ASCORB":1.5,"FERULIC":2,"RESVERATROL":2,"TOCOPHEROL":0.5,"TOCOPHERYL":0.5,"ECTOIN":1}
# 순위: 유분기 (무거운 잔여물 − 휘발성)
GREASY_TOK={"PETROLATUM":3,"MINERAL OIL":3,"PARAFFINUM":3,"LANOLIN":3,"RICINUS":2.5,"CASTOR":2.5,"COCOS NUCIFERA":2,"COCONUT OIL":2,"OLEA":2,"OLIVE":2,"GLYCINE SOJA":2,"SOYBEAN":2,"HELIANTHUS":1.5,"SUNFLOWER":1.5,"SHEA":2,"BUTYROSPERMUM":2,"COCOA BUTTER":2,"THEOBROMA":2,"MANGIFERA":2,"BEESWAX":1.5,"CERA ALBA":1.5,"ISOPROPYL MYRISTATE":1,"JOJOBA":1}
ANTIGREASE_TOK={"CYCLOPENTASILOXANE":2.5,"CYCLOHEXASILOXANE":2.5,"CYCLOMETHICONE":2.5,"ISODODECANE":2.5,"ISOHEXADECANE":2,"ISOEICOSANE":2,"DIMETHICONE":1.5,"SILICA":1.5,"STARCH":1.5,"CAPRYLYL METHICONE":1.5}
# 백탁: 분산 기술
DISPERSE_SILANE=["TRIETHOXYCAPRYLYLSILANE","TRIMETHOXYCAPRYLYLSILANE","HYDROGEN DIMETHICONE"]
DISPERSE_DISP=["POLYHYDROXYSTEARIC","PEG-12 DIMETHICONE","PEG-10 DIMETHICONE"]
# 정제: 비선크림
NONSUN=["DIAPER","RASH","NIPPLE CREAM","WOUND","HEMORRHOID","SUPPOSITOR","ANTIFUNGAL","OBSTETRIX","PRENATAL",
  "DHA COMBO","CICAPAIR","CICA ","TIGER GRASS","COLOR CORRECTING TREATMENT","OINTMENT","PASTE ","SKIN PROTECTANT",
  "ACNE","ANTI-ITCH","FIRST AID","BODY WASH","SHAMPOO","CLEANSER","TOOTHPASTE","DEODORANT"]
LIP=re.compile(r"\bLIP\b|LIPSTICK|CHAPSTICK|LIP BALM|LIP GLOSS|LIP STYLO|LIP OIL")
MAKEUP=re.compile(r"\bMAKEUP\b|FOUNDATION|MASCARA|EYELINER|EYE SHADOW|CONCEALER|\bBLUSH\b|SETTING POWDER|MAKEUP BASE|4 IN 1 MAKEUP|LIQUID MAKEUP|BB CREAM|CC CREAM|CUSHION|SMART SHADE")
JUNK=["NASAL","PROPOLIS NASAL","SOLAR SWIPE","WIPE"]
EMULS=["STEARATE","CETEARYL","POLYSORBATE","LECITHIN","OLIVATE","SORBITAN","PEG-","POLYGLYCERYL","CETYL","STEARYL ALCOHOL","BEHENYL","GLYCERYL"]
BABY1=["BABY","BABIES","INFANT","NEWBORN","PEDIATRIC","TODDLER"]
KIDS2=["KID","KIDS","CHILD","CHILDREN","FAMILY","ALL AGES"]
POS=["SENSITIVE","GENTLE","UNSCENTED","FRAGRANCE FREE","FRAGRANCE-FREE","DELICATE","MILD","HYPOALLERGENIC"]
WORDNUM={'FIFTEEN':15,'TWENTY':20,'THIRTYFIVE':35,'THIRTY':30,'FORTYFIVE':45,'FORTY':40,'FIFTY':50,'SIXTY':60,'SEVENTY':70,'HUNDRED':100}

def norm(s): return re.sub(r"\s+"," ",(s or "").upper()).strip()
def nm(p): return norm((p.get("product_name","") or "")+" "+(p.get("title","") or ""))
def ings(p): return [norm(i.get("name") if isinstance(i,dict) else i) for i in (p.get("inactive_ingredients") or []) if (i.get("name") if isinstance(i,dict) else i)]
def blob(p): return " | ".join(ings(p))
def actives(p): return [norm(a.get("name")) for a in (p.get("active_ingredients") or [])]
def has(b,keys): return any(k in b for k in keys)
def pct(p,which):
    for a in (p.get("active_ingredients") or []):
        if which in norm(a.get("name")):
            v=a.get("percent_ww")
            if v is None: v=a.get("percent")
            return v if isinstance(v,(int,float)) and 0<v<=30 else None
    return None

def spf_of(p):
    s=p.get("spf")
    if isinstance(s,(int,float)) and s: return int(s)
    n=nm(p)
    m=re.search(r'SPF\s*(\d{1,3})',n)
    if m: return int(m.group(1))
    m=re.search(r'\bSPF\b.*?\b(FIFTEEN|TWENTY|THIRTYFIVE|THIRTY|FORTYFIVE|FORTY|FIFTY|SIXTY|SEVENTY|HUNDRED)\b',n)
    if m: return WORDNUM[m.group(1)]
    return None
def is_sun_named(p): return has(nm(p),["SUNSCREEN","SUNBLOCK","SUN SCREEN","SPF","BROAD SPECTRUM","UVA","UVB"])

# ═══════════════ 1. 소스 게이트 + 정제 ═══════════════
COSMETIC_SIG=["WHITENING","BRIGHTENING","VITAMIN C","ANTI-AGING","ANTIAGING","ANTI-WRINKLE","WRINKLE","FIRMING"," BB "," CC ","PRIMER","FOUNDATION"," SERUM ","ESSENCE","TONE UP","TONE-UP","TONEUP","GLOW SERUM","SCAR","NEWGEL","AMPOULE","TINTED MOISTURIZER","COLOR CORRECT","DARK SPOT","LIFTING","PEPTIDE","BIORETINOL","GLOW DROPS","PREP STEP","PERFECTING","PERFECTOR","MATTIFYING","MATTESCREEN","BLURRING","SETTING","PRESSED POWDER","LOOSE POWDER","MINERAL POWDER","POWDER BRUSH","BRUSH-ON","BRUSH ON","MINERAL VEIL","COMPLEXION RESCUE","COMPLEXION","PORE","BRONZING","BRONZER","DAY CREAM","DEFENSE RADIANT","RADIANT PROTECTION","RADIANT DEFENSE","AIRLIGHT","SET SCREEN","MINERAL SHIELD"]
# 파우더 제형 화장품 (흡입+화장품) — powder는 선크림보다 화장품이 대부분
POWDER_COSMETIC=["PRESSED POWDER","LOOSE POWDER","POWDER BRUSH","BRUSH-ON","BRUSH ON","SETTING","MINERAL VEIL","AIRLIGHT"]
# 패턴 기반 (브랜드 아니라 제품유형): 보습제+SPF, 색보정/아이/베일
COSMETIC_PATTERN=["MOISTURIZER SPF","MOISTURIZER BROAD","DAY CREAM","MOISTURISER SPF","CORRECTOR","PALETTE","TOTAL EYE"," EYE ","EYE 3-IN-1","EYE CREAM","TOUCH UP","TOUCH-UP","VEIL","EVEN UP","EVEN-UP","REDNESS","CC CREAM","BB CREAM","DAILY DEFENSE MOISTURIZER","VITAL POWER","SKINLONGEVITY","RENEWAL THERAPY","DAY DEFENSE MOISTURIZER"]
def refine(canonical):
    real=[]; drop=Counter()
    for p in canonical:
        av=" | ".join(actives(p))
        # 미네랄 게이트
        if not any(("ZINC OXIDE" in a or "TITANIUM DIOXIDE" in a) for a in actives(p)): drop["non_mineral"]+=1; continue
        if has(av,CHEM_FILTERS): drop["chemical_filter"]+=1; continue
        # salicylic acid(BHA 각질) 자동배제 — 아기 선크림 부적합 (octisalate/BOS와 구분, word-boundary)
        import re as _re
        if _re.search(r"(?<![A-Z])SALICYLIC ACID", blob(p)): drop["salicylic_acid"]+=1; continue
        # PABA·trolamine salicylate·벌레퇴치제 콤보 자동배제 (AAP/AAD 명시)
        if any(t in blob(p)+av for t in ["AMINOBENZOIC","TROLAMINE SALICYLATE","TRIETHANOLAMINE SALICYLATE"]): drop["paba_trolamine"]+=1; continue
        if any(t in blob(p)+av+nm(p) for t in ["DIETHYLTOLUAMIDE","DEET ","PICARIDIN","IR3535","INSECT REPELLENT"]): drop["repellent_combo"]+=1; continue
        # 광독성 시트러스 자동배제 (bergapten/furocoumarin — 햇빛질문 대체)
        if any(t in blob(p) for t in ["BERGAMOT","CITRUS BERGAMIA","BERGAPTEN","FUROCOUMARIN","PSORALEN","CITRUS AURANTIFOLIA","CITRUS LIMON","CITRUS PARADISI"]): drop["phototoxic_citrus"]+=1; continue
        # ★ 숨은 화학필터: inactive에 등록 화학 UV 필터 있으면 제외 (미네랄 표방인데 화학 숨김)
        # 정밀: butyloctyl/tridecyl salicylate(부스터, _x_boosters 토글 소관)는 제외, word-boundary
        HIDDEN_CHEM=["AVOBENZONE","OXYBENZONE","BENZOPHENONE","OCTINOXATE","OCTYL METHOXYCINNAMATE","OCTOCRYLENE","HOMOSALATE","ENSULIZOLE","MEXORYL","MERADIMATE","PADIMATE","SULISOBENZONE","DIOXYBENZONE","CINOXATE"]
        _ib=blob(p)
        if any(re.search(r"(?<![A-Z])"+re.escape(c), _ib) for c in HIDDEN_CHEM): drop["hidden_chemical_filter"]+=1; continue
        n=nm(p)
        # 정크/비선크림/립/메이크업
        if has(n,JUNK): drop["junk"]+=1; continue
        if LIP.search(n): drop["lip"]+=1; continue
        if MAKEUP.search(n): drop["makeup"]+=1; continue
        if any(sig in " "+n+" " for sig in COSMETIC_SIG): drop["cosmetic_treatment"]+=1; continue
        if any(pat in n for pat in COSMETIC_PATTERN): drop["cosmetic_treatment"]+=1; continue
        if "POWDER" in n and "SUNSCREEN" not in n.replace("POWDER",""): drop["powder_cosmetic"]+=1; continue
        s=spf_of(p)
        if has(n,NONSUN) and not (s and is_sun_named(p)): drop["nonsun"]+=1; continue
        # SPF 게이트 (선크림의 정의)
        if s is None and not is_sun_named(p): drop["no_spf"]+=1; continue
        if len(ings(p))<3: drop["ingredients_lt3"]+=1; continue  # 파싱 결손
        p["_spf"]=s
        real.append(p)
    return real, drop

# ═══════════════ 2. baby_basis 분류 ═══════════════
ADULT_ACT=["RETINOL","RETINYL","TRETINOIN","ADAPALENE","DIHYDROXYACETONE","BAKUCHIOL","ARBUTIN","TRANEXAMIC","PEPTIDE","HYDROQUINONE"]
ADULT_CLAIM=["ANTI-AGING","ANTIAGING","ANTI-WRINKLE","WRINKLE","FIRMING","LIFTING","AGE-DEFYING","MATURE","RESURFAC","DARK SPOT","BRIGHTENING"]
def baby_basis(p):
    n=" "+nm(p)+" "; b=blob(p); full=" | ".join(actives(p))+" | "+b
    if any(re.search(r"(?<![A-Z])"+re.escape(t)+r"(?![A-Z])",n) for t in BABY1): return "labeled_baby"
    if any(re.search(r"(?<![A-Z])"+re.escape(t)+r"(?![A-Z])",n) for t in KIDS2): return "labeled_kids_family"
    if has(full,ADULT_ACT) or has(n,ADULT_CLAIM): return None  # 성인
    if any(s in n for s in POS): return "positive_signal"
    if "TALLOW" in n: return "niche_tallow"
    return "eligible_unlabeled"

# ═══════════════ 3. 배지/플래그 사전계산 ═══════════════
def anhydrous(p): return has(nm(p),["STICK","BALM","BUTTER"]) and not any("WATER" in i or "AQUA" in i for i in ings(p))
def emulsion(p): return any("WATER" in i or "AQUA" in i for i in ings(p))
def truncated_ingredients(p): return emulsion(p) and len([i for i in ings(p) if i])<6 and not has(blob(p),EMULS)

def white_cast(p):
    z=pct(p,"ZINC OXIDE"); t=pct(p,"TITANIUM DIOXIDE")
    if z is None and t is None: return (None,None)
    if z is not None and t is None and z<5: return (None,None)  # 품질게이트: solo ZnO<5% 의심
    load=(z or 0)+1.8*(t or 0); b=blob(p); eng=0
    if has(b,DISPERSE_SILANE): eng+=2
    if "CAPRYLYL METHICONE" in b or ("DIMETHICONE CROSSPOLYMER" in b and CYCLIC.search(b)): eng+=2
    if has(b,DISPERSE_DISP): eng+=1
    disc={0:1.0,1:0.92,2:0.85,3:0.72,4:0.72}.get(min(eng,4),0.72)
    idx=round(load*disc,1)
    return (("LOW" if idx<12 else "MEDIUM" if idx<20 else "HIGH"), idx)

def spreadability(p):
    igs=ings(p)
    if not igs: return (None,None)
    num=w=0
    for i,t in enumerate(igs):
        for tok,val in SV.items():
            if tok in t:
                ww=3 if i<len(igs)/3 else 2 if i<2*len(igs)/3 else 1; num+=val*ww; w+=ww; break
    base=num/w if w else 400
    if any(("WATER" in t or "AQUA" in t) and idx<3 for idx,t in enumerate(igs)): base+=150
    if anhydrous(p): base-=300
    return (("rich" if base<400 else "medium" if base<800 else "light"), round(base))



def barrier_score(p):
    b=blob(p); return round(sum(v for k,v in BARRIER.items() if k in b),1)
def antiox_score(p):
    b=blob(p); return round(sum(v for k,v in ANTIOX.items() if k in b),1)

def greasiness(p):
    igs=ings(p)
    if not igs: return (None,None)
    def posw(tok):
        n=len(igs); sc=0
        for i,t in enumerate(igs):
            if tok in t: sc+= 3 if i<n/3 else (2 if i<2*n/3 else 1)
        return sc
    idx=sum(posw(t)*w for t,w in GREASY_TOK.items())-sum(posw(t)*w for t,w in ANTIGREASE_TOK.items())
    return (("low" if idx<0 else "medium" if idx<15 else "high"), round(idx,1))

def soothe_score(p): return round(sum(v for k,v in SOOTHE.items() if k in blob(p)),1)

def flags(p):
    b=blob(p); full=b+" | "+" | ".join(actives(p))+" | "+nm(p)
    wc,wc_i=white_cast(p); sp,sp_i=spreadability(p)
    n_in=len([i for i in ings(p) if i])
    trunc=truncated_ingredients(p)
    return {
      # 배제 스택 (True = 그 성분 있음 = 켜면 배제됨)
      "_x_boosters": has(full,BOOSTERS),
      "_x_phenoxyethanol": "PHENOXYETHANOL" in b,
      "_x_formaldehyde": has(b,FORMALDEHYDE),
      "_x_mimci": has(b,MIMCI),
      "_x_fragrance": has(b,FRAG),
      "_x_eu_allergen": has(b,EU26),
      "_x_synthetic_dye": bool(__import__("re").search(r"FD&C|D&C|\bYELLOW \d|\bRED \d|\bBLUE \d|CI 1[0-9]{4}|CI 4[0-9]{4}|CI 7[0-9]{4}", b)),
      # 알러지 플래그 (정밀 토큰, 오탐수정 — 부모 알러지 선택시 매칭)
      "_alg_coconut": any(t in b for t in ["COCOS NUCIFERA","COCONUT","COCO-CAPRYLATE","COCO-GLUCOSIDE","COCOATE","COCAMIDE","COCOYL"]),
      "_alg_sunflower": any(t in b for t in ["HELIANTHUS","SUNFLOWER"]),
      "_alg_aloe": any(t in b for t in ["ALOE","BARBADENSIS"]),
      "_alg_shea": any(t in b for t in ["SHEA","BUTYROSPERMUM"]),
      "_alg_jojoba": any(t in b for t in ["JOJOBA","SIMMONDSIA"]),
      "_alg_seed_oil": any(t in b for t in ["RUBUS","RASPBERRY SEED","VITIS","GRAPE SEED","ROSA CANINA","ROSEHIP","OENOTHERA","EVENING PRIMROSE"]),
      "_alg_green_tea": any(t in b for t in ["CAMELLIA SINENSIS","GREEN TEA"]),
      "_alg_tree_nut": any(t in b for t in ["PRUNUS AMYGDALUS","SWEET ALMOND","ARGANIA","ARGAN","MACADAMIA","JUGLANS","WALNUT","CORYLUS","HAZELNUT","ANACARDIUM","CASHEW","PISTACIA VERA"]),
      "_alg_cocoa": any(t in b for t in ["THEOBROMA","COCOA BUTTER"]),
      "_alg_olive": any(t in b for t in ["OLEA EUROPAEA"]),
      "_alg_castor": any(t in b for t in ["RICINUS","CASTOR"]),
      "_alg_avocado": any(t in b for t in ["PERSEA","AVOCADO"]),
      "_alg_citrus": any(t in b for t in ["CITRUS","BERGAMOT"]),
      "_alg_calendula": "CALENDULA" in b,
      "_alg_safflower": any(t in b for t in ["CARTHAMUS","SAFFLOWER"]),
      "_alg_oat": any(t in b for t in ["AVENA","COLLOIDAL OAT","OAT KERNEL"]),
      "_alg_honey_beeswax": any(t in b for t in ["BEESWAX","CERA ALBA","HONEY","PROPOLIS","ROYAL JELLY"]),
      "_alg_lanolin": any(t in b for t in ["LANOLIN","WOOL"]),
      # 식품 알러지 (정제오일 보수적 = 다 매칭, 생명안전)
      "_alg_soy": any(t in b for t in ["GLYCINE SOJA","SOYBEAN","SOJA","HYDROLYZED SOY","SOY PROTEIN","SOY ISOFLAVON","LECITHIN, SOY"]),
      "_alg_wheat": bool(__import__("re").search(r"TRITICUM|HYDROLYZED WHEAT|WHEAT PROTEIN|WHEAT GERM|WHEAT STARCH|WHEAT BRAN|WHEAT AMINO|HORDEUM|SECALE", b)),
      "_alg_peanut": any(t in b for t in ["ARACHIS","PEANUT"]),
      "_alg_milk": any(t in b for t in [" MILK","CASEIN","WHEY","LACTIS PROTEIN"]),
      "_alg_egg": any(t in b for t in [" EGG","ALBUMEN","OVUM","LYSOZYME"]),
      "_alg_sesame": any(t in b for t in ["SESAMUM","SESAME"]),
      "_alg_fish": any(t in b for t in [" FISH","COD LIVER","SALMON OIL"]),
      "_alg_shellfish": any(t in b for t in ["SHELLFISH","CRUSTACEAN","CHITOSAN","CHITIN"]),
      "_x_talc": "TALC" in b,
      "_x_retinol": has(b,RETINOL),
      "_x_spray_powder": has(nm(p),["SPRAY","AEROSOL","MIST","POWDER"]),
      "_x_d456": bool(CYCLIC.search(b)),
      "_x_ethoxylated": bool(ETHOX.search(b)),
      "_x_parabens": "PARABEN" in b,
      "_x_tea": "TRIETHANOLAMINE" in b,
      "_x_bht_bha": has(b,["BUTYLATED HYDROXYTOLUENE","BUTYLATED HYDROXYANISOLE"]),
      "_x_petrolatum": has(b,PETRO),
      "_claim_non_nano": ("NON-NANO" in full or "NON NANO" in full),
      # 순위 지수 (+품질게이트: 신뢰 못하면 None)
      "_r_whitecast": wc, "_r_whitecast_idx": wc_i,
      "_r_spread": sp, "_r_spread_idx": sp_i,
      "_r_greasiness": greasiness(p)[0], "_r_greasiness_idx": greasiness(p)[1],
      "_r_ingredient_count": None if trunc else n_in,   # 잘림이면 순위 제외
      "_r_soothe": soothe_score(p),
      "_r_barrier": barrier_score(p),
      "_r_antiox": antiox_score(p),
      "_ingredient_truncated": trunc,
      # 상황
      "_spf": p.get("_spf"),
      "_form": form_of(p),
      # 안전(강제) — 리콜은 아래 별도
    }

def form_of(p):
    n=nm(p)
    for pat,f in [(r'\bBUTTER\s*STICK\b','stick'),(r'\bSTICK\b','stick'),(r'\bLOTION\b','lotion'),(r'\bCREAM\b','cream'),
      (r'\bBALM\b','balm'),(r'\bSPRAY\b|\bAEROSOL\b|\bMIST\b','spray'),(r'\bPOWDER\b','powder'),(r'\bMILK\b|\bFLUID\b','lotion'),
      (r'\bGEL\b','gel'),(r'\bOINTMENT\b|\bPASTE\b|\bBUTTER\b','balm')]:
        if re.search(pat,n): return f
    return None

# ═══════════════ 4. 리콜 이력 매칭 (제품 라인) ═══════════════
def build_recall_index(recalls):
    rec=recalls if isinstance(recalls,list) else (recalls.get("recalls") or recalls.get("products") or [])
    if isinstance(recalls,dict) and not rec:
        for k,v in recalls.items():
            if isinstance(v,list) and len(v)>10: rec=v;break
    lines=[]
    for r in rec:
        dn=norm((r.get("display_name") or r.get("product_name") or r.get("title") or ""))
        if "SUNSCREEN" in dn or "SPF" in dn:
            # 모든 사유 필드 통합 (benzene은 reason/plain_reason에 있음, hazard엔 contamination만)
            haz=norm(" ".join(str(r.get(k,"")) for k in ["hazard","reason","plain_reason"]) + " " + " ".join(r.get("hazards") or []))
            reasons=set()
            if "BENZ" in haz: reasons.add("benzene")
            if "CONTAM" in haz or "MICROB" in haz or "MOLD" in haz or "BACTER" in haz: reasons.add("contamination")
            if "DEATH" in haz or "FATAL" in haz or bool(r.get("deaths_reported")): reasons.add("death")
            if not reasons: reasons.add("other")
            words=[w for w in re.findall(r'[A-Z]+',dn) if len(w)>3][:3]
            if len(words)>=2:
                lines.append((words[:2], reasons))
    return lines
def recall_flags(p, lines):
    n=nm(p); reasons=set(); hit=False
    for words,rs in lines:
        if all(w in n for w in words):
            hit=True; reasons|=rs
    return {"_recall_history": hit, "_recall_reasons": sorted(reasons)}

# ═══════════════ MAIN ═══════════════
def build(canonical_path, legacy_path, recalls_path, out_path):
    canonical=[json.loads(l) for l in open(canonical_path) if l.strip()]
    legacy=json.load(open(legacy_path)).get("products",[]) if legacy_path else []
    recalls=json.load(open(recalls_path)) if recalls_path else []
    leg_by_setid={p.get("setid"):p for p in legacy if p.get("setid")}
    rec_lines=build_recall_index(recalls)

    real, drop = refine(canonical)
    out=[]; bc=Counter()
    for p in real:
        basis=baby_basis(p)
        rec=dict(p)
        rec["_baby_basis"]=basis
        rec["is_baby_product"]= basis in ("labeled_baby","labeled_kids_family","positive_signal","eligible_unlabeled","niche_tallow")
        rec["_find_eligible"]=True
        rec.update(flags(p))
        rec.update(recall_flags(p, rec_lines))
        # labeler를 SPL title의 [...]에서 파싱 (canonical엔 brand/labeler 없음)
        m=re.search(r'\[([^\]]+)\]\s*$', (p.get("title","") or ""))
        rec["labeler"]=m.group(1).strip() if m else None
        if not rec.get("brand") and rec["labeler"]:
            rec["brand"]=rec["labeler"]  # brand 없으면 labeler로 fallback
        # NDC/wearability 복원 (legacy setid 매칭)
        leg=leg_by_setid.get(p.get("setid"))
        if leg:
            rec["ndc"]=leg.get("ndc")
            for k in ["asin","amazon_url","brand","wearability_score","_feature_badge"]:
                if leg.get(k) is not None: rec.setdefault(k,leg[k])
        bc[basis or "adult/expansion"]+=1
        out.append(rec)

    feed={"schema_version":"2.0","generated_at":datetime.date.today().isoformat(),
          "source":"canonical(percent_ww) + build_v2_feed.py","product_count":len(out),
          "notes":"Full mineral sunscreen pool + precomputed stack/rank/recall/situational flags. App reads flags only.",
          "products":out}
    json.dump(feed, open(out_path,"w"), ensure_ascii=False, indent=1)
    return real, drop, bc, out

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--canonical", default="us_new.jsonl")
    ap.add_argument("--legacy", default="legacy_jul.json")
    ap.add_argument("--recalls", default="recalls.json")
    ap.add_argument("--out", default="/mnt/user-data/outputs/tinysafe_baby_sunscreens.json")
    a=ap.parse_args()
    real,drop,bc,out=build(a.canonical,a.legacy,a.recalls,a.out)
    print("정제 제외:",dict(drop))
    print("진짜 미네랄 선크림:",len(real))
    print("baby_basis:",dict(bc))
    print("출력:",len(out),"→",a.out)
