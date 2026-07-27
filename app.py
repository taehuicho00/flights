"""
아무데나 저가 여행 검색 웹사이트 - 단일 파일 버전

출발일을 정하지 않고, 오늘부터 6개월간 여러 날짜를 자동으로 훑어
인천(ICN)·김포(GMP)에서 '아무데나' 갈 수 있는 곳 중 가장 저렴한 시기를 찾는다.
입력은 여행 일수(N박)와 예산(선택)만 받는다.

실행: 터미널에서  python3 app.py  → 브라우저에서 http://localhost:5000
데이터 출처: RapidAPI 의 Sky-Scrapper API (스카이스캐너 데이터).
"""

import os
import time
from datetime import date, timedelta

import requests
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# ── 설정 ────────────────────────────────────────────────
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
API_HOST = "sky-scrapper.p.rapidapi.com"
BASE_URL = f"https://{API_HOST}"
HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": API_HOST,
}

ORIGIN_AIRPORTS = ["ICN", "GMP"]  # 인천, 김포

# 앞으로 몇 개월을 훑을지. 각 달마다 대표 날짜 1개씩 조회한다.
# (호출 횟수·속도 때문에 촘촘히는 못 훑음. 줄이면 더 빠르고 호출도 아낌.)
SCAN_MONTHS = 6

_airport_cache = {}


def search_airport(query):
    """공항/도시 이름으로 skyId 를 찾는다 (IATA 코드가 아닌 스카이스캐너 자체 코드)."""
    key = query.strip().lower()
    if key in _airport_cache:
        return _airport_cache[key]

    resp = requests.get(
        f"{BASE_URL}/api/v1/flights/searchAirport",
        headers=HEADERS,
        params={"query": query, "locale": "en-US"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return None

    top = data[0]
    result = {"skyId": top["skyId"], "name": top.get("presentation", {}).get("title", query)}
    _airport_cache[key] = result
    return result


def sample_dates(months=SCAN_MONTHS):
    """오늘부터 매월 대표 출발일 목록을 만든다 (대략 30일 간격)."""
    today = date.today()
    return [today + timedelta(days=30 * (i + 1)) for i in range(months)]


def search_everywhere(origin_sky_id, travel_date, return_date):
    """한 출발지에서 특정 날짜에 '아무데나' 갈 수 있는 목적지+최저가 목록."""
    params = {
        "originSkyId": origin_sky_id,
        "travelDate": travel_date.isoformat(),
        "currency": "KRW",
    }
    # 왕복(여행 일수) 반영. 엔드포인트가 무시하면 편도 기준으로 동작한다.
    if return_date is not None:
        params["returnDate"] = return_date.isoformat()

    resp = requests.get(
        f"{BASE_URL}/api/v1/flights/searchFlightEverywhere",
        headers=HEADERS,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def extract_destinations(raw_data, origin_code, travel_date):
    """API 응답에서 (목적지, 가격, 출발일) 목록을 뽑는다. 응답 형태 2가지 모두 대응."""
    results = []
    d = travel_date.isoformat()

    # 형태 A: data = [{Meta:{...}, Payload:{...}}, ...]
    if isinstance(raw_data, list):
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            meta = item.get("Meta", {})
            payload = item.get("Payload", {})
            name = meta.get("CountryNameEnglish") or meta.get("CityName")
            price = payload.get("Price")
            if name and isinstance(price, (int, float)):
                results.append({"origin": origin_code, "name": name,
                                "price_raw": float(price), "date": d})

    # 형태 B: data.everywhereDestination.results = [{content:{...}}, ...]
    elif isinstance(raw_data, dict):
        buckets = raw_data.get("everywhereDestination", {}).get("results", [])
        for item in buckets:
            content = item.get("content", {})
            loc = content.get("location", {})
            quote = content.get("flightQuotes", {}).get("cheapest", {})
            name = loc.get("name")
            price = quote.get("rawPrice")
            if name and isinstance(price, (int, float)):
                results.append({"origin": origin_code, "name": name,
                                "price_raw": float(price), "date": d})

    return results


def format_won(amount):
    return "₩" + format(int(round(amount)), ",")


@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/search")
def api_search():
    """쿼리 파라미터: nights(여행 N박, 필수), budget(예산 상한, 선택)."""
    if not RAPIDAPI_KEY:
        return jsonify({"error": "RAPIDAPI_KEY 환경변수가 설정되지 않았습니다."}), 500

    nights_raw = request.args.get("nights", "").strip()
    budget_raw = request.args.get("budget", "").strip()

    if not nights_raw:
        return jsonify({"error": "여행 일수를 입력하세요."}), 400
    try:
        nights = int(nights_raw)
        if nights < 0:
            raise ValueError
    except ValueError:
        return jsonify({"error": "여행 일수는 0 이상의 정수로 입력하세요."}), 400

    budget = None
    if budget_raw:
        try:
            budget = float(budget_raw)
        except ValueError:
            return jsonify({"error": "예산은 숫자로 입력하세요."}), 400

    try:
        best_by_dest = {}  # {목적지명: 6개월 중 최저가 1건}

        for travel_date in sample_dates():
            return_date = travel_date + timedelta(days=nights) if nights > 0 else None
            for code in ORIGIN_AIRPORTS:
                origin = search_airport(code)
                if origin is None:
                    continue
                raw = search_everywhere(origin["skyId"], travel_date, return_date)
                for dest in extract_destinations(raw, code, travel_date):
                    name = dest["name"]
                    if name not in best_by_dest or dest["price_raw"] < best_by_dest[name]["price_raw"]:
                        best_by_dest[name] = dest
                time.sleep(0.3)

        results = list(best_by_dest.values())
        if budget is not None:
            results = [r for r in results if r["price_raw"] <= budget]

        results.sort(key=lambda r: r["price_raw"])
        for r in results:
            r["price"] = format_won(r["price_raw"])

        return jsonify({
            "nights": nights,
            "budget": format_won(budget) if budget is not None else None,
            "count": len(results),
            "cheapest": results[0] if results else None,
            "destinations": results[:30],
        })

    except requests.HTTPError as e:
        return jsonify({"error": f"API 오류: {e.response.status_code}"}), 502
    except requests.RequestException:
        return jsonify({"error": "네트워크 오류가 발생했습니다. 다시 시도하세요."}), 502


# ── 화면(HTML) ─ 원래 templates/index.html 내용을 여기에 합쳤다 ─────────
HTML_PAGE = """
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>아무데나 · 6개월 최저가로 떠나기</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, "Malgun Gothic", sans-serif; background: #f4f6fb;
      color: #1a1a2e; line-height: 1.6; padding: 24px 16px; }
    .wrap { max-width: 860px; margin: 0 auto; }
    h1 { font-size: 24px; margin-bottom: 4px; }
    .sub { color: #6b7280; font-size: 14px; margin-bottom: 24px; }
    .card { background: #fff; border-radius: 14px; padding: 20px;
      box-shadow: 0 2px 10px rgba(0,0,0,.05); margin-bottom: 20px; }
    .form-row { display: flex; gap: 12px; flex-wrap: wrap; }
    .field { flex: 1; min-width: 180px; }
    label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
    input { width: 100%; padding: 11px 12px; border: 1px solid #d1d5db;
      border-radius: 9px; font-size: 15px; }
    .hint { font-size: 12px; color: #9ca3af; margin-top: 4px; }
    button { margin-top: 16px; width: 100%; padding: 13px; background: #2563eb;
      color: #fff; border: none; border-radius: 9px; font-size: 16px; font-weight: 600; cursor: pointer; }
    button:disabled { background: #9ca3af; cursor: not-allowed; }
    .status { text-align: center; color: #6b7280; padding: 16px; }
    .error { color: #dc2626; }
    .best { background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 12px;
      padding: 16px; margin-bottom: 16px; }
    .best .tag { color: #059669; font-weight: 700; font-size: 13px; }
    .best .place { font-size: 22px; font-weight: 800; color: #065f46; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 12px; }
    .dest { border: 1px solid #eef0f4; border-radius: 11px; padding: 14px; }
    .dest .name { font-weight: 700; font-size: 16px; }
    .dest .p { font-size: 18px; font-weight: 800; color: #2563eb; margin-top: 4px; }
    .dest .meta { font-size: 12px; color: #6b7280; margin-top: 4px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 6px;
      font-size: 12px; font-weight: 600; margin-top: 6px; }
    .icn { background: #dbeafe; color: #1d4ed8; }
    .gmp { background: #fef3c7; color: #b45309; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🌏 아무데나, 6개월 안에 제일 싸게</h1>
    <p class="sub">앞으로 6개월을 훑어 인천·김포에서 가장 저렴하게 갈 수 있는 곳을 찾아드립니다.</p>
    <div class="card">
      <div class="form-row">
        <div class="field">
          <label for="nights">여행 일수</label>
          <input id="nights" type="number" min="0" placeholder="예: 3" />
          <p class="hint">N박 (0이면 편도)</p>
        </div>
        <div class="field">
          <label for="budget">예산 (선택)</label>
          <input id="budget" type="number" placeholder="예: 300000" />
          <p class="hint">원(KRW) · 비우면 전부 표시</p>
        </div>
      </div>
      <button id="searchBtn">가장 싼 곳 찾기</button>
      <p class="hint" style="margin-top:10px">※ 6개월을 훑느라 최대 1~2분 걸릴 수 있어요.</p>
    </div>
    <div id="result"></div>
  </div>
  <script>
    const btn = document.getElementById("searchBtn");
    const resultEl = document.getElementById("result");
    btn.addEventListener("click", search);

    async function search() {
      const nights = document.getElementById("nights").value.trim();
      const budget = document.getElementById("budget").value.trim();
      if (nights === "") {
        resultEl.innerHTML = '<p class="status error">여행 일수를 입력하세요.</p>';
        return;
      }
      btn.disabled = true;
      resultEl.innerHTML = '<p class="status">6개월치를 훑는 중입니다… 1~2분 걸릴 수 있어요.</p>';
      try {
        let url = `/api/search?nights=${encodeURIComponent(nights)}`;
        if (budget !== "") url += `&budget=${encodeURIComponent(budget)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) {
          resultEl.innerHTML = `<p class="status error">${data.error || "오류가 발생했습니다."}</p>`;
          return;
        }
        render(data);
      } catch (e) {
        resultEl.innerHTML = '<p class="status error">요청에 실패했습니다. 서버가 켜져 있는지 확인하세요.</p>';
      } finally {
        btn.disabled = false;
      }
    }

    function render(data) {
      if (!data.destinations || data.destinations.length === 0) {
        resultEl.innerHTML = '<p class="status">조건에 맞는 목적지를 찾지 못했습니다. 예산을 올리거나 여행 일수를 바꿔보세요.</p>';
        return;
      }
      const b = data.cheapest;
      const bestHtml = `
        <div class="best">
          <div class="tag">6개월 중 가장 싼 곳</div>
          <div class="place">${b.name} · ${b.price}</div>
          <div class="meta">${originText(b.origin)} · 출발 ${b.date}${nightsText(data.nights)}</div>
        </div>`;
      const cards = data.destinations.map(d => `
        <div class="dest">
          <div class="name">${d.name}</div>
          <div class="p">${d.price}</div>
          <div class="meta">출발 ${d.date}${nightsText(data.nights)}</div>
          ${originBadge(d.origin)}
        </div>`).join("");
      const budgetText = data.budget ? `${data.budget} 이하 · ` : "";
      resultEl.innerHTML = `
        <div class="card">
          ${bestHtml}
          <p class="sub">${budgetText}총 ${data.count}곳 (가격순) · 조회 ${new Date().toLocaleString("ko-KR")}</p>
          <div class="grid">${cards}</div>
          <p class="sub" style="margin-top:12px">※ 6개월 중 매월 대표 날짜만 표본 조회한 결과라, 실제 최저가는 더 낮을 수 있습니다.</p>
        </div>`;
    }
    function nightsText(n) { return n > 0 ? ` · ${n}박` : " · 편도"; }
    function originText(code) { return code === "ICN" ? "인천 출발" : "김포 출발"; }
    function originBadge(code) {
      const cls = code === "ICN" ? "icn" : "gmp";
      return `<span class="badge ${cls}">${originText(code)}</span>`;
    }
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
