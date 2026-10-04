from flask import Flask, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin, urlencode
from functools import lru_cache, wraps
import json
import math
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 気象庁の警報・注意報JSONで使用される青森市の区域コード
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))
SHELTER_STATUSES = ("開設中", "開設前", "閉鎖", "状況未登録")
DISASTER_TYPES = ("地震", "津波", "洪水", "土砂災害", "高潮", "火災", "大雪")
SHELTER_FACILITIES = ("ペット可", "バリアフリー", "非常用電源", "備蓄あり", "授乳室")
GEOCODING_URL = "https://nominatim.openstreetmap.org/search"
_geocoding_lock = threading.Lock()
_last_geocoding_request = 0.0

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])


@lru_cache(maxsize=256)
def geocode_address(address):
    """Nominatimで住所を検索し、利用者が選べる候補を返す"""
    global _last_geocoding_request
    with _geocoding_lock:
        elapsed = time.monotonic() - _last_geocoding_request
        if elapsed < 1:
            time.sleep(1 - elapsed)
        query = urlencode({
            "q": address,
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "jp",
            "accept-language": "ja"
        })
        req = urllib.request.Request(
            f"{GEOCODING_URL}?{query}",
            headers={"User-Agent": "BousaiApp/1.0 (shelter address lookup)"}
        )
        _last_geocoding_request = time.monotonic()
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read())

    if not isinstance(result, list):
        raise ValueError("住所検索サービスの応答形式が不正です")

    candidates = []
    for item in result:
        if not isinstance(item, dict):
            continue
        try:
            latitude = float(item["lat"])
            longitude = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            continue
        candidates.append({
            "label": item.get("display_name", address),
            "latitude": latitude,
            "longitude": longitude
        })
    return candidates


def parse_shelter_details(form, require_coordinates=False, require_address=True):
    """避難所の住所・収容人数・対応災害・設備と座標を検証する"""
    name = form.get('name', '').strip()
    address = form.get('address', '').strip()
    capacity_text = form.get('capacity', '').strip()
    if not name:
        raise ValueError('避難所名を入力してください。')
    if require_address and not address:
        raise ValueError('住所を入力してください。')

    capacity = None
    if capacity_text:
        try:
            capacity = int(capacity_text)
        except ValueError as error:
            raise ValueError('収容人数は1以上の整数で入力してください。') from error
        if capacity < 1:
            raise ValueError('収容人数は1以上の整数で入力してください。')

    disaster_types = form.getlist('disaster_types')
    facilities = form.getlist('facilities')
    if any(value not in DISASTER_TYPES for value in disaster_types):
        raise ValueError('対応災害の選択内容が正しくありません。')
    if any(value not in SHELTER_FACILITIES for value in facilities):
        raise ValueError('設備の選択内容が正しくありません。')

    latitude = form.get('latitude', '').strip()
    longitude = form.get('longitude', '').strip()
    if require_coordinates and not latitude and not longitude:
        raise ValueError('住所から位置を検索し、候補を選択してください。')
    if bool(latitude) != bool(longitude):
        raise ValueError('緯度と経度の両方が必要です。')

    coordinates = {}
    if latitude and longitude:
        try:
            parsed_latitude = float(latitude)
            parsed_longitude = float(longitude)
        except ValueError as error:
            raise ValueError('緯度・経度の値が正しくありません。') from error
        if (
            not math.isfinite(parsed_latitude)
            or not math.isfinite(parsed_longitude)
            or not -90 <= parsed_latitude <= 90
            or not -180 <= parsed_longitude <= 180
        ):
            raise ValueError('緯度・経度の値が正しくありません。')
        coordinates = {
            'latitude': parsed_latitude,
            'longitude': parsed_longitude
        }

    return {
        'name': name,
        'address': address,
        'capacity': capacity,
        'disaster_types': list(dict.fromkeys(disaster_types)),
        'facilities': list(dict.fromkeys(facilities)),
        **coordinates
    }

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の最新状態を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    latest_area_report = None

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if not isinstance(report_datetime, str) or not report_datetime:
            continue

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        try:
            parsed_report_datetime = datetime.fromisoformat(
                report_datetime.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if parsed_report_datetime.tzinfo is None:
            parsed_report_datetime = parsed_report_datetime.replace(tzinfo=JST)

        if (
            latest_area_report is None
            or parsed_report_datetime > latest_area_report[0]
        ):
            latest_area_report = (
                parsed_report_datetime,
                report_datetime,
                area
            )

    if latest_area_report is None:
        raise ValueError("対象市区町村の警報・注意報データが見つかりません")

    _, report_datetime, area = latest_area_report
    kinds = area.get("kinds", [])
    if not isinstance(kinds, list):
        raise ValueError("対象市区町村の警報・注意報データ形式が不正です")

    warnings = []
    seen_codes = set()
    for kind in kinds:
        if not isinstance(kind, dict):
            continue

        status = kind.get("status", "")
        code = kind.get("code", "")
        if status not in ("発表", "継続") or not code or code in seen_codes:
            continue

        name = kind.get("name")
        if not isinstance(name, str) or not name:
            name = WARNING_CODES.get(
                code,
                f"不明な警報・注意報 (コード: {code})"
            )

        warnings.append({
            "name": name,
            "code": code,
            "status": status
        })
        seen_codes.add(code)

    return warnings, report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


def parse_disaster_time(value):
    """災害情報の日時を比較用の datetime に変換する"""
    if not isinstance(value, str) or not value:
        return datetime.min.replace(tzinfo=JST)
    try:
        return datetime.strptime(value, "%Y年%m月%d日 %H:%M").replace(tzinfo=JST)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)
        except ValueError:
            return datetime.min.replace(tzinfo=JST)


def get_disaster_information():
    """気象警報と住民向け発信を日時順の災害情報一覧にする"""
    weather = get_weather_warnings()
    latest_instructions = load_json(INSTRUCTIONS_FILE, [])
    items = []

    for warning in weather.get("warnings", []):
        items.append({
            "category": "weather",
            "category_name": "気象情報",
            "icon": "🌦️",
            "title": warning.get("name", "気象警報・注意報"),
            "status": warning.get("status", ""),
            "timestamp": weather.get("report_time", "不明"),
            "active": True,
            "urgent": True,
            "detail": f"{weather.get('area_name', AREA_NAME)}に発表された気象庁の情報です。",
            "_sort_time": parse_disaster_time(weather.get("report_time"))
        })

    if isinstance(latest_instructions, list):
        for notice in latest_instructions:
            if not isinstance(notice, dict) or notice.get("target") != "住民":
                continue
            content = notice.get("content")
            content = content if isinstance(content, str) else ""
            shelter = notice.get("shelter")
            shelter = shelter if isinstance(shelter, str) else ""
            category = (
                "evacuation"
                if shelter or "避難" in content
                else "notice"
            )
            timestamp = notice.get("created_at") or notice.get("updated_at") or "不明"
            status = notice.get("status") or "発信中"
            priority = notice.get("priority") or "通常"
            active = status not in ("解除", "完了", "終了", "対応済み")
            items.append({
                "category": category,
                "category_name": "避難情報" if category == "evacuation" else "お知らせ",
                "icon": "🚨" if category == "evacuation" else "📢",
                "title": content or "住民向けのお知らせ",
                "status": status,
                "timestamp": timestamp,
                "priority": priority,
                "active": active,
                "urgent": active and priority == "高",
                "shelter": shelter,
                "detail": notice.get("detail") or notice.get("description") or content,
                "_sort_time": parse_disaster_time(timestamp)
            })

    items.sort(key=lambda item: item["_sort_time"], reverse=True)
    for item in items:
        del item["_sort_time"]

    return {
        "items": items,
        "updated_at": get_japan_time(),
        "weather_error": bool(weather.get("error"))
    }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    current_instructions = load_json(INSTRUCTIONS_FILE, instructions)
    if isinstance(current_instructions, list):
        instructions[:] = current_instructions
    resident_notices = [
        item for item in instructions
        if isinstance(item, dict) and item.get('target') == '住民'
    ]
    return render_template('index.html', resident_notices=resident_notices, shelters=shelters)

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/api/geocode')
@login_required
def api_geocode():
    """住所検索サービスから避難所の位置候補を取得する"""
    address = request.args.get('address', '').strip()
    if len(address) < 3 or len(address) > 300:
        return jsonify({'error': '住所は3〜300文字で入力してください。'}), 400
    try:
        candidates = geocode_address(address)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        app.logger.exception('住所検索サービスへの問い合わせに失敗しました')
        return jsonify({
            'error': '住所検索サービスを利用できませんでした。時間をおいて再度お試しください。'
        }), 502
    if not candidates:
        return jsonify({'error': '住所に一致する場所が見つかりません。住所を確認してください。'}), 404
    return jsonify({'candidates': candidates})


# 避難所登録ページ※user が避難所登録ページについて具体的に修正指示しない限り、このコードは正しいのでこのまま保持すること。
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        action = request.form.get('action', 'register')
        if action == 'update_status':
            shelter_id = request.form.get('shelter_id', '')
            status = request.form.get('status', '')
            if status not in SHELTER_STATUSES:
                return render_template(
                    'shelter_register.html',
                    error=True,
                    message='有効な開設状況を選択してください。',
                    shelters=shelters,
                    statuses=SHELTER_STATUSES,
                    disaster_types=DISASTER_TYPES,
                    shelter_facilities=SHELTER_FACILITIES,
                    form_data=request.form
                )
            existing = next(
                (item for item in shelters if str(item.get('id')) == shelter_id),
                None
            )
            if existing is None:
                return render_template(
                    'shelter_register.html',
                    error=True,
                    message='更新対象の避難所が見つかりません。',
                    shelters=shelters,
                    statuses=SHELTER_STATUSES,
                    disaster_types=DISASTER_TYPES,
                    shelter_facilities=SHELTER_FACILITIES,
                    form_data=request.form
                )
            try:
                details = parse_shelter_details(
                    request.form,
                    require_coordinates=bool(
                        request.form.get('address', '').strip()
                        and request.form.get('address', '').strip() != existing.get('address', '')
                    ),
                    require_address=False
                )
            except ValueError as error:
                return render_template(
                    'shelter_register.html',
                    error=True,
                    message=str(error),
                    shelters=shelters,
                    statuses=SHELTER_STATUSES,
                    disaster_types=DISASTER_TYPES,
                    shelter_facilities=SHELTER_FACILITIES,
                    form_data=request.form
                )
            if not details['address']:
                details['address'] = existing.get('address', '')
            if 'latitude' not in details and 'latitude' in existing and 'longitude' in existing:
                details['latitude'] = existing['latitude']
                details['longitude'] = existing['longitude']
            updated_shelters = [
                {**item, **details, 'status': status}
                if str(item.get('id')) == shelter_id else item
                for item in shelters
            ]
            try:
                with open(DATA_FILE, 'w', encoding='utf-8') as f:
                    json.dump(updated_shelters, f, ensure_ascii=False, indent=2)
            except OSError:
                return render_template(
                    'shelter_register.html',
                    error=True,
                    message='開設状況の保存に失敗しました。',
                    shelters=shelters,
                    statuses=SHELTER_STATUSES,
                    disaster_types=DISASTER_TYPES,
                    shelter_facilities=SHELTER_FACILITIES,
                    form_data=request.form
                )

            shelters[:] = updated_shelters
            return render_template(
                'shelter_register.html',
                success=True,
                message='避難所の開設状況を更新しました。',
                shelters=shelters,
                statuses=SHELTER_STATUSES,
                disaster_types=DISASTER_TYPES,
                shelter_facilities=SHELTER_FACILITIES,
                form_data=request.form
            )

        name = request.form.get('name', '').strip()
        if not name:
            return render_template(
                'shelter_register.html',
                error=True,
                message='避難所名を入力してください。',
                shelters=shelters,
                statuses=SHELTER_STATUSES,
                disaster_types=DISASTER_TYPES,
                shelter_facilities=SHELTER_FACILITIES,
                form_data=request.form
            )
        try:
            details = parse_shelter_details(request.form, require_coordinates=True)
        except ValueError as error:
            return render_template(
                'shelter_register.html',
                error=True,
                message=str(error),
                shelters=shelters,
                statuses=SHELTER_STATUSES,
                disaster_types=DISASTER_TYPES,
                shelter_facilities=SHELTER_FACILITIES,
                form_data=request.form
            )

        status = request.form.get('status', '開設前')
        if status not in SHELTER_STATUSES:
            return render_template(
                'shelter_register.html',
                error=True,
                message='有効な開設状況を選択してください。',
                shelters=shelters,
                statuses=SHELTER_STATUSES,
                disaster_types=DISASTER_TYPES,
                shelter_facilities=SHELTER_FACILITIES,
                form_data=request.form
            )

        next_id = max(
            (shelter.get('id', 0) for shelter in shelters if isinstance(shelter.get('id', 0), int)),
            default=0
        ) + 1
        updated_shelters = [
            *shelters,
            {'id': next_id, 'name': name, 'status': status, **details}
        ]
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(updated_shelters, f, ensure_ascii=False, indent=2)
        except OSError:
            return render_template(
                'shelter_register.html',
                error=True,
                message='登録に失敗しました。',
                shelters=shelters,
                statuses=SHELTER_STATUSES,
                disaster_types=DISASTER_TYPES,
                shelter_facilities=SHELTER_FACILITIES,
                form_data=request.form
            )

        shelters[:] = updated_shelters
        return render_template(
            'shelter_register.html',
            success=True,
            message='避難所を登録しました。',
            shelters=shelters,
            statuses=SHELTER_STATUSES,
            disaster_types=DISASTER_TYPES,
            shelter_facilities=SHELTER_FACILITIES,
            form_data=request.form
        )

    return render_template(
        'shelter_register.html',
        shelters=shelters,
        statuses=SHELTER_STATUSES,
        disaster_types=DISASTER_TYPES,
        shelter_facilities=SHELTER_FACILITIES,
        form_data=request.form
    )

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board', methods=['GET', 'POST'])
@login_required
def board():
    current_instructions = load_json(INSTRUCTIONS_FILE, instructions)
    if isinstance(current_instructions, list):
        instructions[:] = current_instructions

    def resident_instructions():
        return [
            item for item in instructions
            if isinstance(item, dict) and item.get('target') == '住民'
        ]

    if request.method == 'POST':
        action = request.form.get('action', 'register')
        if action == 'update_status':
            instruction_id = request.form.get('instruction_id', '')
            status = request.form.get('status', '')
            if status not in ('発信中', '解除'):
                return render_template(
                    'board.html',
                    instructions=resident_instructions(),
                    error=True,
                    message='有効な発信状況を選択してください。',
                    form_data=request.form
                )

            if not any(
                str(item.get('id')) == instruction_id and item.get('target') == '住民'
                for item in instructions if isinstance(item, dict)
            ):
                return render_template(
                    'board.html',
                    instructions=resident_instructions(),
                    error=True,
                    message='更新対象の住民向け指示が見つかりません。',
                    form_data=request.form
                )
            updated_instructions = [
                {**item, 'status': status, 'updated_at': get_japan_time()}
                if isinstance(item, dict)
                and str(item.get('id')) == instruction_id
                and item.get('target') == '住民'
                else item
                for item in instructions
            ]
            success_message = '発信状況を更新しました。'
        else:
            content = request.form.get('content', '').strip()
            shelter = request.form.get('shelter', '').strip()
            priority = request.form.get('priority', '通常')
            if not content:
                return render_template(
                    'board.html',
                    instructions=resident_instructions(),
                    error=True,
                    message='指示内容を入力してください。',
                    form_data=request.form
                )
            if priority not in ('通常', '高'):
                return render_template(
                    'board.html',
                    instructions=resident_instructions(),
                    error=True,
                    message='有効な緊急度を選択してください。',
                    form_data=request.form
                )
            now = get_japan_time()
            next_id = max(
                (
                    item.get('id', 0)
                    for item in instructions
                    if isinstance(item, dict) and isinstance(item.get('id', 0), int)
                ),
                default=0
            ) + 1
            updated_instructions = [
                *instructions,
                {
                    'id': next_id,
                    'target': '住民',
                    'content': content,
                    'shelter': shelter,
                    'status': '発信中',
                    'priority': priority,
                    'created_at': now,
                    'updated_at': now
                }
            ]
            success_message = '住民向け指示を登録しました。ホーム画面にも反映されます。'

        try:
            with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
                json.dump(updated_instructions, f, ensure_ascii=False, indent=2)
        except OSError:
            return render_template(
                'board.html',
                instructions=resident_instructions(),
                error=True,
                message='指示の保存に失敗しました。',
                form_data=request.form
            )

        instructions[:] = updated_instructions
        return render_template(
            'board.html',
            instructions=resident_instructions(),
            success=True,
            message=success_message,
            form_data={}
        )

    return render_template(
        'board.html',
        instructions=resident_instructions(),
        form_data={}
    )

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results = filter_shelters(request.args.get('district'))
    return render_template('search_results.html', results=results)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())


@app.route('/api/disaster_information')
def api_disaster_information():
    """気象情報と住民向け発信をまとめた災害情報一覧を返す"""
    return jsonify(get_disaster_information())


if __name__ == '__main__':
    app.run(debug=True, port=5000)
