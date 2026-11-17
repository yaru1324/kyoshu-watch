"""教習所(obic7モバイル予約)の空き枠を監視し、新しく空いた枠をメールで通知する。

必要な環境変数:
  START_URL          お気に入り登録ページ(「ログインページへ」が出るページ)のURL
  KYOSHU_ID          教習生番号
  KYOSHU_PASSWORD    パスワード
  GMAIL_USER         送信元Gmailアドレス
  GMAIL_APP_PASSWORD Gmailのアプリパスワード
  MAIL_TO            通知先メールアドレス
任意:
  MAX_WEEKS          何週間分見るか (既定 4)
  HEADLESS           0 にするとブラウザを表示 (既定 1)
  STATE_FILE         前回結果の保存先 (既定 state.json)
"""
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

STATE = Path(os.environ.get("STATE_FILE", "state.json"))
MAX_WEEKS = int(os.environ.get("MAX_WEEKS", "4"))
HEADLESS = os.environ.get("HEADLESS", "1") != "0"

# 日付の行: #lst_lc 直下で、バッジを持つもの(時限詳細の .blocks は対象外)
ROW = "#lst_lc > .page > div:has(> .blocks .badge)"


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"環境変数 {name} が未設定です")
    return v


# ---------- iframe対応のヘルパー ----------
def find_frame(page, selector, timeout=30):
    """全フレームから selector が見えているフレームを探す。"""
    end = time.time() + timeout
    while time.time() < end:
        for fr in page.frames:
            try:
                loc = fr.locator(selector).first
                if loc.count() and loc.is_visible():
                    return fr
            except Exception:
                pass
        time.sleep(0.5)
    raise TimeoutError(f"見つかりません: {selector}")


def find(page, selector, timeout=30):
    return find_frame(page, selector, timeout).locator(selector).first


# ---------- 画面操作 ----------
def login(page):
    page.goto(env("START_URL"), wait_until="domcontentloaded")
    find(page, 'text="ログインページへ"').click()
    find(page, 'input[placeholder*="教習生番号"]').fill(env("KYOSHU_ID"))
    find(page, 'input[type="password"]').fill(env("KYOSHU_PASSWORD"))
    find(page, 'text="ログイン"').click()
    find(page, 'text="教習予約"').click()
    find(page, "#lst_lc .badge")  # 予約枠ページの表示待ち


def first_date(page):
    fr = find_frame(page, "#lst_lc .badge")
    return fr.locator(ROW).first.locator(".lbl").first.inner_text().strip()


def scrape_week(page):
    """表示中の1週間分から、空いている時限を {"日付|時限": "開始～終了"} で返す。"""
    fr = find_frame(page, "#lst_lc .badge")
    rows = fr.locator(ROW)
    n = rows.count()
    if n == 0:
        raise RuntimeError("日付の行が見つかりません(画面構造が変わった可能性)")
    result = {}
    for i in range(n):
        row = rows.nth(i)
        date = row.locator(".blocks > div:first-child .lbl").first.inner_text().strip()
        badge = row.locator(".badge").first.inner_text().strip()
        if badge != "空":
            continue
        panel = row.locator("xpath=following-sibling::div[1]")
        if panel.locator(".blocks").count() == 0:
            row.locator(".blocks").first.click()  # 展開すると時限一覧が読み込まれる
            try:
                panel.locator(".blocks").first.wait_for(timeout=10000)
            except PWTimeout:
                print(f"警告: {date} の時限一覧を取得できませんでした")
                continue
        for b in panel.locator(".blocks").all():
            t = [s.strip() for s in b.locator(".lbl").all_inner_texts()]
            if len(t) >= 5:  # ["01","時限","09:00","～","09:50", ""]
                result[f"{date}|{t[0]}時限"] = f"{t[2]}～{t[4]}"
    return result


def wait_week_changed(page, before, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if first_date(page) != before:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def collect_all():
    slots = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 430, "height": 900},
        )
        ctx.set_default_timeout(30000)
        page = ctx.new_page()
        try:
            login(page)
            for w in range(MAX_WEEKS):
                before = first_date(page)
                slots.update(scrape_week(page))
                if w == MAX_WEEKS - 1:
                    break
                try:
                    find(page, "label[for=btnNextWeek]", timeout=5).click()
                except TimeoutError:
                    break  # 次の週ボタンが無い = 最終週
                if not wait_week_changed(page, before):
                    break  # 日付が変わらない = これ以上先は無い
        except Exception:
            if not HEADLESS:
                page.screenshot(path="error.png")
            raise
        finally:
            browser.close()
    return slots


# ---------- 通知 ----------
def format_slots(slots):
    by_date = {}
    for key, tm in sorted(slots.items()):
        date, period = key.split("|")
        by_date.setdefault(date, []).append(f"  {period} {tm}")
    return "\n".join(f"{d}\n" + "\n".join(v) for d, v in by_date.items())


def send_mail(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = env("GMAIL_USER")
    msg["To"] = env("MAIL_TO")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(env("GMAIL_USER"), env("GMAIL_APP_PASSWORD"))
        s.send_message(msg)


def main():
    slots = collect_all()
    print(f"取得した空き枠: {len(slots)}件")

    prev = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    url = env("START_URL")

    if prev is None:
        body = "監視を開始しました。現在の空き枠:\n\n" + (format_slots(slots) or "(空きなし)")
        send_mail("【教習予約】監視を開始しました", body + f"\n\n{url}")
    else:
        new = {k: v for k, v in slots.items() if k not in prev}
        if new:
            send_mail(
                f"【教習予約】空きが出ました({len(new)}枠)",
                "新しく空いた枠:\n\n" + format_slots(new) + f"\n\n{url}",
            )
            print(f"通知しました: {len(new)}枠")

    STATE.write_text(json.dumps(slots, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
