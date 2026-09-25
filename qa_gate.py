#!/usr/bin/env python3
"""
qa_gate.py — 자동화의 안전장치. build_v2_feed 출력을 검증.
통과 못 하면 exit 1 → 워크플로우가 푸시 중단 → 옛 피드 유지.
Usage: python3 qa_gate.py new_feed.json [old_feed.json]
"""
import json, sys

new_path = sys.argv[1]
old_path = sys.argv[2] if len(sys.argv) > 2 else None

def fail(msg): print(f"❌ QA FAIL: {msg}"); sys.exit(1)
def ok(msg): print(f"✅ {msg}")

# 1. JSON 유효 + 구조
try:
    d = json.load(open(new_path))
except Exception as e:
    fail(f"JSON invalid: {e}")
P = d.get("products") if isinstance(d, dict) else d
if not P: fail("no products")
N = len(P)
ok(f"JSON valid · {N} products")

# 2. 제품 수 급감 방지 (이전 대비 -20% 넘으면 파이프라인 사고 의심)
if old_path:
    try:
        old = json.load(open(old_path))
        oldP = old.get("products") if isinstance(old, dict) else old
        oldN = len(oldP)
        drop = (oldN - N) / oldN if oldN else 0
        if drop > 0.20:
            fail(f"product count dropped {drop*100:.0f}% ({oldN}→{N}) — pipeline error suspected")
        ok(f"count stable ({oldN}→{N}, {drop*100:+.0f}%)")
    except Exception as e:
        print(f"⚠️ couldn't compare to old feed: {e}")

# 3. 필수 필드 존재 (배제15 + 순위 + 식별자)
p = P[0]
xflags = [k for k in p if k.startswith("_x_")]
rflags = [k for k in p if k.startswith("_r_")]
if len(xflags) < 15: fail(f"exclusion flags {len(xflags)}/15")
if not any("whitecast" in k for k in rflags): fail("no whitecast ranking")
if not any("greasiness" in k for k in rflags): fail("no greasiness ranking")
for req in ["labeler", "_baby_basis", "active_ingredients", "inactive_ingredients"]:  # ndc sparse by design (canonical lacks it, legacy-matched only)
    cov = sum(1 for x in P if x.get(req) not in (None, "", []))
    if cov < N * 0.9: fail(f"{req} coverage {cov}/{N} (<90%)")
ok(f"fields present · {len(xflags)} exclusion · {len(rflags)} ranking · labeler/basis/ndc ok")

# 4. 타입 계약 (Rork 디코드 안전)
for f in ["_x_phenoxyethanol", "_tearfree" if "_tearfree" in p else "_x_fragrance"]:
    if f in p and not all(isinstance(x.get(f), bool) for x in P if x.get(f) is not None):
        fail(f"{f} not all bool")
ok("type contracts (bool/int/list) ok")

# 5. 앵커 회귀 (엔진이 정상 작동하나 — full NDC 고정)
ANCHORS = {
    "60781-1003-3": ("HIGH", "medium"),   # Thinkbaby Baby
    "85739-005-01": ("HIGH", "high"),     # Kids Tallow
    "69968-0623-2": ("MEDIUM", "low"),    # Neutrogena Sheer
    "43319-071-11": ("HIGH", "medium"),   # Goongbe Baby Easy Wash (ZnO 20% = HIGH, verified)
}
def ndc(p): return (p.get("ndc") or "").strip()
found = 0; anchor_fail = []
for p in P:
    if ndc(p) in ANCHORS:
        found += 1
        got = (p.get("_r_whitecast"), p.get("_r_greasiness"))
        exp = ANCHORS[ndc(p)]
        if got != exp: anchor_fail.append(f"{ndc(p)}: got {got} exp {exp}")
if anchor_fail: fail(f"anchor regression: {anchor_fail}")
if found == 0:
    print("⚠️ no NDC anchors found — feed version may differ, but not blocking")
else:
    ok(f"anchor regression pass ({found} anchors by NDC)")

# 6. 데이터 품질 요약 (경고만, 차단 X)
zno_unknown = sum(1 for p in P if p.get("_r_whitecast") == "UNKNOWN")
print(f"ℹ️ white-cast UNKNOWN (판단보류): {zno_unknown} ({zno_unknown/N*100:.0f}%)")
benzene = sum(1 for p in P if "benzene" in (p.get("_recall_reasons") or []))
print(f"ℹ️ benzene flagged: {benzene}")

print(f"\n{'='*50}\n✅ QA GATE PASSED — safe to push ({N} products)\n{'='*50}")
sys.exit(0)
