#!/usr/bin/env python3
"""
나의 책장 — 어드민 서버 (내 컴퓨터에서만 돌아간다, 파이썬 표준 라이브러리만 쓴다)

이 폴더는 두 쪽으로 나뉜다.
  어드민  admin/     내 컴퓨터에서만 여는 관리 화면. 책을 찾고 등록하고 고친다
  사이트  docs/      GitHub Pages 가 웹에 올리는 쪽. shelf.json 하나만 읽어서 책장을 그린다

데이터가 흐르는 길
  어드민에서 등록·수정  →  data/books.json (내 기록 원본)
  어드민의 [게시] 버튼  →  docs/shelf.json (사이트가 읽는 파일)
  git push            →  웹에 반영

[게시]를 따로 둔 이유. 어드민에서 고치는 중인 것이 바로 웹에 나가지 않게 하려고.
고치고, 로컬 미리보기로 확인하고, 게시하고, push 하는 네 걸음이다.

YES24 API 키는 .env 에만 둔다. 키는 이 서버가 YES24 에 물어볼 때만 쓰고
브라우저로 내려보내지 않는다. .env 는 .gitignore 에 들어 있어서 GitHub 에 올라가지 않는다.

실행
    python3 admin/server.py          # http://127.0.0.1:8799 어드민, /site/ 는 사이트 미리보기
    python3 admin/server.py 9000     # 포트 지정
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # 책장 폴더 맨 바깥
ADMIN = ROOT / "admin"
DOCS = ROOT / "docs"                               # GitHub Pages 가 여는 폴더
BOOKS_PATH = ROOT / "data" / "books.json"          # 내 기록 원본
SHELF_PATH = DOCS / "shelf.json"                   # 사이트가 읽는 파일
ENV_PATH = ROOT / ".env"
DEFAULT_PORT = 8799

CATEGORIES = ["문학", "에세이", "인문", "사회", "과학", "실용", "기타"]


# ───────────────────────────── ① 설정과 저장 ─────────────────────────────
def read_env():
    """ .env 의 KEY=값 줄을 읽는다. 환경변수에 같은 이름이 있으면 그쪽이 먼저다. """
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    if os.environ.get("YES24_API_KEY"):
        env["YES24_API_KEY"] = os.environ["YES24_API_KEY"]
    return env


def load_db():
    if not BOOKS_PATH.exists():
        return {"shelf": {"title": "나의 책장", "owner": ""}, "books": []}
    return json.loads(BOOKS_PATH.read_text(encoding="utf-8"))


def save_db(db):
    # 임시 파일에 먼저 쓰고 바꿔치기한다. 쓰다가 꺼져도 원본이 반쯤 지워지지 않게.
    BOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BOOKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(BOOKS_PATH)


# ───────────────────────────── ② YES24 ─────────────────────────────
# 기본 키 한도는 초당 10회, 하루 20,000회. 내 책을 찾을 때만 부르고, 서점 전체를 긁어 쌓지 않는다(이용 조건).
_last_call = [0.0]
_lock = threading.Lock()


def yes24(path, params):
    key = read_env().get("YES24_API_KEY", "")
    if not key or key.startswith("여기에"):
        raise RuntimeError(".env 파일에 YES24_API_KEY 가 없어요. README 의 'YES24 키 넣기'를 따라 해 주세요")
    url = "https://apis.yes24.com/v1" + path + "?" + urllib.parse.urlencode(params)
    with _lock:  # 호출 사이를 0.15초 띄워 한도를 넘지 않게 한다
        wait = 0.15 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    req = urllib.request.Request(url, headers={"X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        if e.code in (401, 403):
            raise RuntimeError("YES24 가 키를 받아 주지 않아요. .env 의 키를 다시 확인해 주세요")
        raise RuntimeError(f"YES24 응답 오류 {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"YES24 에 연결하지 못했어요 ({e.reason}). 이 문장을 Claude 에게 보여 주세요")
    if not body or not body.get("success"):
        raise RuntimeError("YES24 응답 오류 " + str((body or {}).get("message", "")))
    return [to_item(it) for it in (body.get("data") or {}).get("items") or []]


def to_item(it):
    """ YES24 응답에서 책장이 쓰는 값만 남긴다. """
    return {
        "yes24Id": it.get("itemId"),
        "isbn13": it.get("isbn13") or "",
        "title": it.get("title") or "",
        "author": clean_author(it.get("author")),
        "publisher": it.get("publisher") or "",
        "media": "전자책" if it.get("goodsType") == "eBook" else "종이책",
        "publishDate": it.get("publishDate") or "",
        "cover": it.get("cover") or "",
        "link": it.get("link") or "",
    }


def clean_author(raw):
    # "어니스트 헤밍웨이 저/김욱동 역" → "어니스트 헤밍웨이"
    first = (raw or "").split("/")[0].strip()
    return re.sub(r"\s+(공?저|글|편저?|지음|원저|글그림|그림|엮음|역|공?편)$", "", first).strip()


def search(q):
    """ 검색어 종류를 알아서 나눈다. YES24 상품 주소, ISBN 13자리, 그 밖엔 제목 검색. """
    q = (q or "").strip()
    if not q:
        return []
    m = re.search(r"yes24\.com/(?:product/)?goods/(\d+)", q, re.I)
    if m:
        return yes24("/goods/itemDetail", {"searchType": "ITEMID", "query": m.group(1)})
    isbn = re.sub(r"[-\s]", "", q)
    if re.fullmatch(r"97[89]\d{10}", isbn):
        return yes24("/goods/itemDetail", {"searchType": "ISBN13", "query": isbn})
    # YES24 검색은 특수문자와 긴 검색어를 거절한다. 글자·숫자·공백만 남기고 60자에서 자른다
    clean = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", q)
    clean = re.sub(r"\s+", " ", clean).strip()[:60]
    return yes24("/goods/itemList", {"query": clean, "page": 1, "pageSize": 12})


# ───────────────────────────── ③ 기록 다루기 ─────────────────────────────
# 기록 한 권에 들어가는 칸. 어드민 입력 화면과 사이트가 이 이름을 같이 쓴다.
FIELDS = ["title", "author", "publisher", "cover", "link", "yes24Id", "isbn13", "media",
          "year", "month", "category", "fav", "review", "quote"]


def clean_book(data, old=None):
    book = dict(old or {})
    for k in FIELDS:
        if k in data:
            book[k] = data[k]
    book["title"] = (book.get("title") or "").strip()
    if not book["title"]:
        raise ValueError("제목이 비어 있어요")
    today = date.today()
    book["year"] = int(book.get("year") or today.year)
    book["month"] = int(book.get("month") or 0) or None
    book["fav"] = bool(book.get("fav"))
    for k in ("review", "quote", "author", "publisher", "category"):
        book[k] = (book.get(k) or "").strip()
    return book


def new_id(db):
    return max([b.get("id", 0) for b in db["books"]] + [0]) + 1


def publish(db):
    """ 사이트가 읽는 docs/shelf.json 을 새로 쓴다. 사이트에 필요한 칸만 내보낸다. """
    keep = ["id", "title", "author", "publisher", "cover", "link", "media",
            "year", "month", "category", "fav", "review", "quote"]
    books = sorted(db["books"], key=lambda b: (b.get("year") or 0, b.get("month") or 0, b.get("id")), reverse=True)
    out = {
        "shelf": db.get("shelf", {}),
        "updated": date.today().isoformat(),
        "books": [{k: b.get(k) for k in keep} for b in books],
    }
    DOCS.mkdir(exist_ok=True)
    SHELF_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return len(books)


def published_differs(db):
    """ 어드민에서 고친 뒤 아직 게시하지 않은 게 있는지. 어드민 위쪽 알림에 쓴다. """
    if not SHELF_PATH.exists():
        return True
    try:
        pub = json.loads(SHELF_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return True
    ids = lambda books: sorted((b.get("id"), json.dumps(b, sort_keys=True, ensure_ascii=False)) for b in books)
    keep = ["id", "title", "author", "publisher", "cover", "link", "media",
            "year", "month", "category", "fav", "review", "quote"]
    mine = [{k: b.get(k) for k in keep} for b in db["books"]]
    return ids(mine) != ids(pub.get("books", [])) or pub.get("shelf") != db.get("shelf")


# ───────────────────────────── ④ 서버 ─────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 터미널을 조용하게
        pass

    def send_json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def send_file(self, path):
        if not path.is_file():
            self.send_error(404)
            return
        types = {".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/":
                return self.send_file(ADMIN / "admin.html")
            if u.path == "/site":
                self.send_response(301)
                self.send_header("Location", "/site/")
                self.end_headers()
                return
            if u.path.startswith("/site/"):
                # 사이트 미리보기. docs/ 를 그대로 보여 준다. 웹에 올라가면 이 화면이 그대로 열린다
                rel = urllib.parse.unquote(u.path[len("/site/"):]) or "index.html"
                target = (DOCS / rel).resolve()
                if DOCS.resolve() not in target.parents and target != DOCS.resolve():
                    return self.send_error(403)
                return self.send_file(target)
            if u.path == "/api/books":
                db = load_db()
                return self.send_json({**db, "categories": CATEGORIES, "unpublished": published_differs(db),
                                       "hasKey": bool(read_env().get("YES24_API_KEY", "").strip()) and not read_env().get("YES24_API_KEY", "").startswith("여기에")})
            if u.path == "/api/search":
                q = urllib.parse.parse_qs(u.query).get("q", [""])[0]
                return self.send_json({"items": search(q)})
            self.send_error(404)
        except Exception as e:  # 화면에 원인을 그대로 보여 준다. 막히면 그 문장을 Claude 에게 보여 주면 된다
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            db = load_db()
            if u.path == "/api/books":
                book = clean_book(self.read_body())
                book["id"] = new_id(db)
                book["addedAt"] = date.today().isoformat()
                db["books"].append(book)
                save_db(db)
                return self.send_json(book)
            if u.path == "/api/shelf":
                data = self.read_body()
                db["shelf"] = {"title": (data.get("title") or "나의 책장").strip(), "owner": (data.get("owner") or "").strip()}
                save_db(db)
                return self.send_json(db["shelf"])
            if u.path == "/api/publish":
                return self.send_json({"count": publish(db)})
            if u.path == "/api/clear-samples":
                db["books"] = [b for b in db["books"] if not b.get("sample")]
                save_db(db)
                return self.send_json({"count": len(db["books"])})
            self.send_error(404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_PUT(self):
        m = re.fullmatch(r"/api/books/(\d+)", urllib.parse.urlparse(self.path).path)
        if not m:
            return self.send_error(404)
        try:
            db = load_db()
            i = next((i for i, b in enumerate(db["books"]) if b["id"] == int(m.group(1))), None)
            if i is None:
                return self.send_json({"error": "없는 책이에요"}, 404)
            db["books"][i] = clean_book(self.read_body(), db["books"][i])
            db["books"][i].pop("sample", None)  # 손댄 샘플은 내 책으로 본다
            save_db(db)
            self.send_json(db["books"][i])
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/books/(\d+)", urllib.parse.urlparse(self.path).path)
        if not m:
            return self.send_error(404)
        db = load_db()
        db["books"] = [b for b in db["books"] if b["id"] != int(m.group(1))]
        save_db(db)
        self.send_json({"ok": True})


def main():
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else DEFAULT_PORT
    # 127.0.0.1 에만 연다. 같은 와이파이의 다른 사람이 내 어드민에 들어올 수 없게
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  나의 책장 어드민   {url}\n  사이트 미리보기    {url}site/\n\n  끄려면 이 창에서 Ctrl+C\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  어드민을 껐어요")


if __name__ == "__main__":
    main()
