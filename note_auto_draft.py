# -*- coding: utf-8 -*-
"""
note_auto_draft.py
Seleniumでnote.comにログインし、記事の下書き保存を自動化する。
※ 投稿(公開)ボタンは押さない。最終確認・公開は必ず自分の目で見て手動で行うこと。

事前準備:
    pip install selenium

使い方:
    1) 初回ログイン(手動でログインしてブラウザのプロフィールに記憶させる):
        python note_auto_draft.py setup

    2) 投稿画面の構造を確認する(初回のみ・エラーが出た場合の調査用):
        python note_auto_draft.py explore

    3) 下書き保存を実行する:
        python note_auto_draft.py post note_articles/note_article_20260802.txt

出力:
    explore_note_page.html  ← exploreコマンド実行時のページソース(調査用)
"""
import sys
import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service

PROFILE_DIR = os.path.abspath("./chrome_profile_note")
NEW_NOTE_URL = "https://note.com/notes/new"
LOGIN_URL = "https://note.com/login"
COOKIES_FILE = os.environ.get("NOTE_COOKIES_PATH", "note_cookies.json")


def _clean_stale_profile_locks():
    """
    Chromeがクラッシュ/強制終了すると、user-data-dir内にロックファイル
    (SingletonLock/SingletonSocket/SingletonCookie)が残ることがあり、
    残ったままだと次回以降ずっと『DevToolsActivePort file doesn't exist』で
    起動失敗し続ける。起動前に必ず掃除しておく。
    """
    if not os.path.isdir(PROFILE_DIR):
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.remove(path)
        except OSError:
            pass  # 掃除に失敗しても起動自体は試す


def make_driver(headless=False):
    """
    ログイン方式は自動判定する:
    - note_cookies.json (または環境変数 NOTE_COOKIES_PATH で指定したファイル)が存在すれば、
      それを使ってCookieログインする(GitHub Actionsなど、永続プロフィールを持てない環境向け)
    - 無ければ、従来通りローカルの永続プロフィール(chrome_profile_note)を使う
    """
    use_cookies = os.path.exists(COOKIES_FILE)

    options = webdriver.ChromeOptions()
    if not use_cookies:
        _clean_stale_profile_locks()
        options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        options.add_argument("--profile-directory=Default")
    if headless:
        options.add_argument("--headless=new")
        # 【2026-09-03追加】headlessだとnote.comの/newページが毎回スケルトン表示のまま
        # 進まないという事象を確認。headless ChromeのデフォルトUAには"HeadlessChrome"
        # という文字列が含まれ、サイト側で検知されて挙動が変わる(読み込みが止まる)
        # 可能性があるため、通常のChromeと同じUAを明示的に指定する。
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    options.add_argument("--window-size=1280,1000")
    # Windows環境でのクラッシュ対策
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    # GitHub Actions等、CHROME_PATH/CHROMEDRIVER_PATH環境変数が設定されている場合はそれを優先する。
    # (指定しないとランナーに元から入っているシステムChromeが使われ、setup-chromeアクションで
    #  インストールしたChromeDriverとバージョンが食い違うことがあるため)
    # ローカルのWindows環境ではこれらの環境変数は設定していないので、従来通りの自動検出で動く。
    chrome_path = os.environ.get("CHROME_PATH")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chrome_path:
        options.binary_location = chrome_path

    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
        driver = webdriver.Chrome(options=options, service=service)
    else:
        # Selenium 4.6+ なら chromedriver は自動で用意される
        driver = webdriver.Chrome(options=options)

    if use_cookies:
        _load_cookies(driver, COOKIES_FILE)

    return driver


def _load_cookies(driver, cookies_path):
    """
    Cookieファイルを読み込み、ブラウザに適用してログイン状態にする。

    【2026-09-11再修正】ドメインごとにページを開いてから add_cookie() する方式を
    試みたが、editor.note.com には「/」だけで開ける独立したページが無い
    (すぐnote.comにリダイレクトされる等)ため、結局そのドメイン分のCookie
    (fp, _vid_v2)が適用できず、Cookie 4/6件のまま変化しなかった。

    そこで、ページ遷移に依存しない Chrome DevTools Protocol(CDP)の
    Network.setCookie を使う方式に切り替えた。これはブラウザが今どのページを
    開いているかに関係なく、任意のドメインのCookieを直接設定できる。
    """
    import json
    try:
        with open(cookies_path, encoding="utf-8") as f:
            cookies = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠ Cookieファイルの読み込みに失敗しました: {e}")
        return False

    # CDPコマンドを使うにも何かページが開いている必要があるため、まず開いておく
    driver.get("https://note.com/")
    time.sleep(1)

    applied = 0
    total = len(cookies)
    for c in cookies:
        cdp_cookie = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        same_site = str(c.get("sameSite") or "").capitalize()
        if same_site in ("Strict", "Lax", "None"):
            cdp_cookie["sameSite"] = same_site
        if c.get("expiry"):
            cdp_cookie["expires"] = c["expiry"]  # CDPは"expires"というキー名(秒単位はexpiryと同じ)
        try:
            driver.execute_cdp_cmd("Network.setCookie", cdp_cookie)
            applied += 1
        except Exception as e:
            print(f"  ⚠ Cookie '{c.get('name')}' の適用に失敗しました: {type(e).__name__}: {e}")

    driver.get("https://note.com/")
    time.sleep(2)
    print(f"  🍪 Cookie {applied}/{total}件を適用しました(CDP経由)。")
    return True


def cmd_setup():
    """初回ログイン用。ブラウザを開いたまま、手動でログインしてもらう。"""
    print(f"プロフィール保存先: {PROFILE_DIR}")
    driver = make_driver(headless=False)
    driver.get(LOGIN_URL)
    print("\nブラウザでnoteにログインしてください。")
    print("ログインが完了したら、このコンソールに戻って Enter キーを押してください。")
    input(">>> ログイン完了後、Enterキーを押してください... ")
    driver.get(NEW_NOTE_URL)
    time.sleep(3)
    print(f"現在のURL: {driver.current_url}")
    print("投稿画面が表示されていればログイン状態は保存されました。")
    input(">>> 確認できたら Enterキーを押すとブラウザを閉じます... ")
    driver.quit()


def cmd_explore():
    """投稿画面のDOM構造を調査してファイルに保存する(セレクタ特定用)。"""
    driver = make_driver(headless=False)
    driver.get(NEW_NOTE_URL)
    time.sleep(4)
    print(f"現在のURL: {driver.current_url}")

    if "login" in driver.current_url:
        print("⚠ ログインしていないようです。先に `python note_auto_draft.py setup` を実行してください。")
        driver.quit()
        return

    # ページソースを保存(手動で見てもらうため)
    with open("explore_note_page.html", "w", encoding="utf-8") as f:
        f.write(driver.page_source)
    print("✅ explore_note_page.html にページソースを保存しました。")

    # 編集可能な要素を洗い出して表示する
    print("\n--- contenteditable要素 ---")
    editable = driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
    for i, el in enumerate(editable):
        print(f"[{i}] tag={el.tag_name} class={el.get_attribute('class')!r} "
              f"aria-label={el.get_attribute('aria-label')!r} "
              f"data-placeholder={el.get_attribute('data-placeholder')!r}")

    print("\n--- textarea要素 ---")
    textareas = driver.find_elements(By.TAG_NAME, "textarea")
    for i, el in enumerate(textareas):
        print(f"[{i}] class={el.get_attribute('class')!r} "
              f"placeholder={el.get_attribute('placeholder')!r}")

    print("\n--- ボタン要素(「下書き」「保存」を含むもの) ---")
    buttons = driver.find_elements(By.TAG_NAME, "button")
    for i, el in enumerate(buttons):
        text = el.text.strip()
        if any(k in text for k in ["下書き", "保存", "公開"]):
            print(f"[{i}] text={text!r} class={el.get_attribute('class')!r}")

    input("\n>>> 確認できたら Enterキーを押すとブラウザを閉じます... ")
    driver.quit()


def load_article(path):
    with open(path, encoding="utf-8") as f:
        content = f.read()
    parts = content.split("\n\n", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else ""
    return title, body


def _draft_url_state_path(for_date=None):
    from datetime import date
    d = for_date or date.today().strftime("%Y%m%d")
    return f"note_draft_url_{d}.txt"


def _robust_click(driver, el):
    """スクロールしてからクリックする。通常クリックが失敗したらJSクリックにフォールバックする。"""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.3)
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def _clear_and_type(driver, el, text, is_contenteditable=False):
    """既存の内容を全選択して削除してから、新しい内容を入力する。"""
    _robust_click(driver, el)
    time.sleep(0.3)
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(Keys.DELETE)
    time.sleep(0.3)
    if is_contenteditable:
        for line in text.split("\n"):
            el.send_keys(line)
            el.send_keys(Keys.ENTER)
    else:
        el.send_keys(text)


def _click_save_draft(driver):
    """「下書き保存」ボタンを明示的に探してクリックする。自動保存に頼らない。"""
    candidates = driver.find_elements(By.XPATH, "//*[contains(text(),'下書き保存')]")
    for el in candidates:
        try:
            _robust_click(driver, el)
            time.sleep(2)
            return True
        except Exception:
            continue
    return False


def cmd_update(article_path, headless=True):
    """
    既存の下書き(今日の日付で以前に作成したもの)があれば開いて上書き更新し、
    無ければ新規作成する。全自動運用向けに対話プロンプトは出さない。
    """
    if not os.path.exists(article_path):
        print(f"エラー: {article_path} が見つかりません。")
        return False

    title, body = load_article(article_path)

    state_path = _draft_url_state_path()
    existing_url = None
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            existing_url = f.read().strip()

    driver = make_driver(headless=headless)
    try:
        target_url = existing_url if existing_url else NEW_NOTE_URL
        driver.get(target_url)
        time.sleep(3)

        if "login" in driver.current_url:
            print("⚠ ログインしていません。`python note_auto_draft.py setup` を先に実行してください。")
            return False

        # 既存下書きURLが無効になっていた場合(削除された等)、新規作成にフォールバック
        if existing_url and "notes" not in driver.current_url:
            driver.get(NEW_NOTE_URL)
            time.sleep(3)

        try:
            title_el = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable((By.TAG_NAME, "textarea")))
            _clear_and_type(driver, title_el, title, is_contenteditable=False)
        except Exception as e:
            print(f"⚠ タイトル欄への入力に失敗しました: {type(e).__name__}: {e}")
            print(f"   (現在のURL: {driver.current_url})")
            return False

        try:
            editable = driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
            if not editable:
                raise Exception("contenteditable要素が見つかりません")
            body_el = editable[0]
            _clear_and_type(driver, body_el, body, is_contenteditable=True)
        except Exception as e:
            print(f"⚠ 本文欄への入力に失敗しました: {type(e).__name__}: {e}")
            return False

        # 「下書き保存」ボタンを明示的にクリックする(自動保存に頼らない)
        time.sleep(1)
        saved = _click_save_draft(driver)
        if not saved:
            print("⚠ 「下書き保存」ボタンが見つかりませんでした。保存されていない可能性があります。")
            return False

        # 下書きのURLを保存しておく(次回の更新で同じ下書きを開くため)
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(driver.current_url)

        print(f"✅ 下書きを更新しました: {driver.current_url}")
        return True
    finally:
        driver.quit()


def _dump_failure_debug(driver, reason):
    """
    失敗時にページソースと現在URLを固定ファイルに保存する(調査用・毎回上書き)。
    auto_predict_cycle.py は create_new_draft の例外メッセージを
    そのまま auto_cycle_log.txt に書き込むので、ここで保存したファイルパスを
    例外メッセージに含めておけば、次に失敗したときログから直接たどれる。
    """
    debug_path = os.path.abspath("note_draft_fail_debug.html")
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        debug_path = "(保存も失敗)"
    current_url = None
    try:
        current_url = driver.current_url
    except Exception:
        current_url = "(取得失敗)"
    return f"{reason} / 現在URL: {current_url} / ページソース保存先: {debug_path}"


def _insert_paywall_line_after(driver, anchor_text, log=None):
    """
    【2026-09-04変更】当初はHTML5ドラッグでpaywall-lineを移動させる方式を試みたが、
    実機確認の結果、新規記事にはそもそも#paywall-line要素が最初から存在せず、
    「/」を入力すると出るブロック挿入メニューから「有料エリア指定」を選んで
    初めて挿入されることが分かった。そのため、ドラッグ方式は廃止し、
    こちらのスラッシュコマンド方式に置き換えた。

    本文中の anchor_text を含む段落の末尾にカーソルを置き、Enterで新しい段落を
    作ってから「/」を入力してブロック挿入メニューを開き、「有料エリア指定」を
    クリックする。

    失敗しても例外は投げない(呼び出し元は下書き作成自体は続行させる)。
    戻り値: 成功したと思われる場合True、そうでなければFalse。
    """
    def _log(msg):
        print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    try:
        body_root = driver.find_element(By.CSS_SELECTOR, ".ProseMirror.note-common-styles__textnote-body")
        paragraphs = body_root.find_elements(By.XPATH, ".//p")
    except Exception:
        _log("  ⚠ 本文の段落要素が取得できませんでした。有料エリア挿入をスキップします。")
        return False

    anchor_el = None
    for p in paragraphs:
        if anchor_text in p.text:
            anchor_el = p
            break

    if anchor_el is None:
        _log("  ⚠ 有料ラインの目印テキストが本文中に見つかりませんでした。有料エリア挿入をスキップします。")
        return False

    try:
        _robust_click(driver, anchor_el)
        time.sleep(0.3)
        anchor_el.send_keys(Keys.END)
        anchor_el.send_keys(Keys.ENTER)
        time.sleep(0.3)
        anchor_el.send_keys("/")
        time.sleep(1.0)

        menu_item = None
        for el in driver.find_elements(By.XPATH, "//*[text()='有料エリア指定']"):
            menu_item = el
            break
        if menu_item is None:
            _log("  ⚠ 「有料エリア指定」メニュー項目が見つかりませんでした。有料エリア挿入をスキップします。")
            return False

        _robust_click(driver, menu_item)
        time.sleep(0.8)
        _log("  💴 有料エリアを目印段落の直後に挿入しました。")
        return True
    except Exception as e:
        _log(f"  ⚠ 有料エリア挿入処理でエラー: {type(e).__name__}: {e}")
        return False


def _click_publish_and_verify(driver, log=None):
    """
    「投稿する」ボタンをクリックし、公開できたかを確認する共通処理。
    【2026-09-11強化】
    - クリック後に確認ダイアログ(モーダル)が出るケースに備え、「投稿する」以外に
      「公開する」「確認」「OK」「はい」といった確認ボタンが新たに現れていないか
      追加で探し、あれば押す。
    - 判定までの待ち時間を伸ばし、間隔を空けて複数回URLをチェックする。
    - 公開できたか確認できなかった場合、ページソースをデバッグ用に保存する。

    戻り値: 公開できたと確認できればTrue。
    """
    def _log(msg):
        print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    submit_btn = None
    for el in driver.find_elements(By.TAG_NAME, "button"):
        if el.text.strip() == "投稿する":
            submit_btn = el
            break
    if submit_btn is None:
        _log("  ⚠ 「投稿する」ボタンが見つかりませんでした。公開は行わず下書きのままにします。")
        return False

    url_before = driver.current_url
    submit_btn.click()
    time.sleep(2)

    # クリック後に確認ダイアログが出ていないか探し、あれば押す
    try:
        confirm_texts = ("公開する", "確認", "OK", "はい")
        for el in driver.find_elements(By.TAG_NAME, "button"):
            t = el.text.strip()
            if t in confirm_texts:
                el.click()
                _log(f"  ➡ 確認ダイアログ(「{t}」)が出たため押しました。")
                time.sleep(2)
                break
    except Exception:
        pass

    # 判定までしばらく待ち、複数回チェックする(反映に時間がかかる場合があるため)
    for _ in range(3):
        time.sleep(2)
        url_after = driver.current_url
        if url_after != url_before and "/publish/" not in url_after:
            _log(f"  🚀 記事を公開しました: {url_after}")
            return True

    url_after = driver.current_url
    debug_path = os.path.abspath("note_publish_fail_debug.html")
    try:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        debug_path = "(保存も失敗)"
    _log(f"  ⚠ 「投稿する」をクリックしましたが、公開できたか確認できませんでした"
         f"(URL変化なし: {url_after})。下書きのまま残っている可能性があります。"
         f"ページソース保存先: {debug_path}")
    return False


def _prepare_paid_settings(driver, price=300, log=None, publish=False):
    """
    「公開に進む」→「有料」に切り替え→価格入力、まで自動で行う。

    publish: 【2026-09-11追加】Trueを渡した場合のみ、最後に「投稿する」ボタンも
      クリックして実際に公開する。Falseなら(デフォルト)従来通り、価格設定までで
      止まり、「投稿する」は絶対にクリックしない。

    戻り値: (settings_ok, published)
      settings_ok: 価格設定までが成功したか
      published: 実際に「投稿する」をクリックし、公開が確認できたか
                 (publish=Falseなら常にFalse)
    """
    def _log(msg):
        print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    try:
        publish_btn = None
        for el in driver.find_elements(By.TAG_NAME, "button"):
            if el.text.strip() == "公開に進む":
                publish_btn = el
                break
        if publish_btn is None:
            _log("  ⚠ 「公開に進む」ボタンが見つかりませんでした。価格設定をスキップします。")
            return False, False
        publish_btn.click()
        time.sleep(2)

        paid_radio = None
        for el in driver.find_elements(By.CSS_SELECTOR, "input[name='is_paid']"):
            if el.get_attribute("value") == "paid":
                paid_radio = el
                break
        if paid_radio is None:
            _log("  ⚠ 「有料」のラジオボタンが見つかりませんでした。価格設定をスキップします。")
            return False, False
        driver.execute_script("arguments[0].click();", paid_radio)
        time.sleep(1)

        # 価格欄は「有料」選択後に現れる、placeholder="300"のテキスト入力欄
        price_input = None
        for el in driver.find_elements(By.TAG_NAME, "input"):
            if el.get_attribute("type") == "text" and el.get_attribute("placeholder") == "300":
                price_input = el
                break
        if price_input is None:
            _log("  ⚠ 価格入力欄が見つかりませんでした。")
            return False, False
        price_input.click()
        # 既存の値を全選択して上書き(単純なclear()はReact管理の入力欄で
        # 効かないことがあるため、Ctrl+Aで全選択してから入力する)
        price_input.send_keys(Keys.CONTROL, "a")
        price_input.send_keys(str(price))
        time.sleep(0.5)

        if not publish:
            _log(f"  💰 有料({price}円)に設定しました。※「投稿する」はまだ押していません。")
            return True, False

        # ここから先はpublish=Trueのときだけ実行される、実際の公開操作
        published = _click_publish_and_verify(driver, log=log)
        return True, published
    except Exception as e:
        _log(f"  ⚠ 価格設定・公開処理でエラー: {type(e).__name__}: {e}")
        return False, False


def _publish_free_article(driver, log=None):
    """
    【2026-09-11追加】無料記事(有料エリアなし)を「公開に進む」→「投稿する」まで
    自動で公開する。price設定は行わない(デフォルトの「無料」のまま)。

    戻り値: 公開できたと確認できればTrue。
    """
    def _log(msg):
        print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    try:
        publish_btn = None
        for el in driver.find_elements(By.TAG_NAME, "button"):
            if el.text.strip() == "公開に進む":
                publish_btn = el
                break
        if publish_btn is None:
            _log("  ⚠ 「公開に進む」ボタンが見つかりませんでした。公開をスキップします。")
            return False
        publish_btn.click()
        time.sleep(2)

        return _click_publish_and_verify(driver, log=log)
    except Exception as e:
        _log(f"  ⚠ 公開処理でエラー: {type(e).__name__}: {e}")
        return False


def create_new_draft(title, body, headless=True, driver=None, log=None,
                      paywall_anchor_text=None, price=300, auto_publish=False):
    """
    タイトル・本文を直接受け取り、常に新規の下書きを1件作成する。
    (1レース1記事运用向け。対話プロンプトは出さない)

    driver: 既存のSeleniumドライバーを渡すと、それを使い回す(呼び出し元がquitを管理する)。
            Noneなら内部で新規作成し、この関数の中でquitまで行う。
    log: 呼び出し元のログ関数(例: auto_predict_cycle.pyのlog())。渡された場合、
         進捗メッセージはprint()だけでなくそちらにも記録される。渡さなければprint()のみ。
    paywall_anchor_text: 渡された場合、本文中でこのテキストを含む段落の直後に有料エリアを
         挿入し、続けて「公開に進む」→「有料」に切り替え→price円を入力するところまで
         自動で行う。Noneの場合は下書き保存のみ行う(従来通り)。
    price: paywall_anchor_textを渡した場合の価格(円)。デフォルト300円。
    auto_publish: 【2026-09-11追加】Trueの場合、下書き保存の後に実際の「投稿する」まで
         クリックして公開する。paywall_anchor_textがある場合は価格設定後に、無い場合
         (無料記事)はそのまま公開する。デフォルトFalse(=下書きのまま、安全側)。
         実際に公開される操作を伴うため、有効にする前にheadless=Falseで一度
         目視確認してから使うことを強く推奨する。

    戻り値: 成功した場合は下書き(または公開後)の記事URL。
    【2026-09-02 修正】失敗時は None を返して黙って諦めるのではなく、原因を
    含めた RuntimeError を送出するようにした。呼び出し元(auto_predict_cycle.py)
    は既に例外を except して traceback ごとログに残しているため、この変更だけで
    「何が原因で失敗したか」が auto_cycle_log.txt に残るようになる。
    """
    def _log(msg):
        print(msg)
        if log:
            try:
                log(msg)
            except Exception:
                pass

    owns_driver = driver is None
    if owns_driver:
        driver = make_driver(headless=headless)
    try:
        driver.get(NEW_NOTE_URL)
        time.sleep(5)  # 新規ノート作成は既存ノートを開くより時間がかかることがある

        if "login" in driver.current_url:
            raise RuntimeError(_dump_failure_debug(
                driver, "ログインしていません(Cookieが切れている可能性)"))

        title_el = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                title_el = WebDriverWait(driver, 25).until(
                    EC.element_to_be_clickable((By.TAG_NAME, "textarea")))
                break
            except Exception as e:
                if attempt < max_attempts - 1:
                    _log(f"  ⏳ タイトル欄がまだ表示されないため、再読み込みして再試行します...(試行{attempt+1}/{max_attempts})")
                    driver.refresh()
                    time.sleep(3)
                else:
                    raise RuntimeError(_dump_failure_debug(
                        driver, f"タイトル欄への入力に失敗: {type(e).__name__}: {e}")) from e
        try:
            _clear_and_type(driver, title_el, title, is_contenteditable=False)
        except Exception as e:
            raise RuntimeError(_dump_failure_debug(
                driver, f"タイトル欄への入力に失敗: {type(e).__name__}: {e}")) from e

        try:
            editable = driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
            if not editable:
                raise Exception("contenteditable要素が見つかりません")
            body_el = editable[0]
            _clear_and_type(driver, body_el, body, is_contenteditable=True)
        except Exception as e:
            raise RuntimeError(_dump_failure_debug(
                driver, f"本文欄への入力に失敗: {type(e).__name__}: {e}")) from e

        # 【2026-09-04追加・実験的】有料ラインの位置調整。失敗しても下書き作成自体は続行する
        # (どのみち最終公開は人間が目視するので、位置がズレていても後で手動修正できる)。
        if paywall_anchor_text:
            _insert_paywall_line_after(driver, paywall_anchor_text, log=log)

        time.sleep(1)
        saved = _click_save_draft(driver)
        if not saved:
            raise RuntimeError(_dump_failure_debug(
                driver, "「下書き保存」ボタンが見つかりませんでした"))

        # 価格設定(・任意で公開)。paywall_anchor_textが渡された時だけ価格設定フローに入る
        # (=有料記事として運用する想定のときだけ)。失敗しても下書き自体は
        # 既に保存済みなので、URLは返す(価格は後で手動設定すればよい)。
        published = False
        if paywall_anchor_text:
            settings_ok, published = _prepare_paid_settings(
                driver, price=price, log=log, publish=auto_publish)
            if not published:
                # 公開しなかった(=下書きのまま)場合のみ、価格設定の過程で
                # 「公開に進む」を押して画面遷移した分を戻しておく
                # (呼び出し元に返すURLを一貫させるため)。
                try:
                    driver.get(f"https://editor.note.com/notes/{_extract_note_id(driver.current_url)}/edit/")
                    time.sleep(1)
                except Exception:
                    pass
        elif auto_publish:
            # paywall_anchor_textが無い(=無料記事)場合の公開パス
            published = _publish_free_article(driver, log=log)
            if not published:
                try:
                    driver.get(f"https://editor.note.com/notes/{_extract_note_id(driver.current_url)}/edit/")
                    time.sleep(1)
                except Exception:
                    pass

        if published:
            _log(f"🚀 記事を公開しました: {driver.current_url}")
        else:
            _log(f"✅ 新規下書きを作成しました: {driver.current_url}")
        return driver.current_url
    finally:
        if owns_driver:
            driver.quit()


def _extract_note_id(url):
    """URLからnoteのID(例: na273a950521e)を抜き出す。見つからなければURLをそのまま返す。"""
    parts = [p for p in url.split("/") if p]
    return next((p for p in parts if p.startswith("n") and len(p) > 8), url)


def cmd_post(article_path):
    if not os.path.exists(article_path):
        print(f"エラー: {article_path} が見つかりません。")
        return

    title, body = load_article(article_path)
    print(f"タイトル: {title}")
    print(f"本文文字数: {len(body)}")

    driver = make_driver(headless=False)
    driver.get(NEW_NOTE_URL)

    wait = WebDriverWait(driver, 15)
    time.sleep(3)

    if "login" in driver.current_url:
        print("⚠ ログインしていません。先に `python note_auto_draft.py setup` を実行してください。")
        driver.quit()
        return

    try:
        # タイトル欄: textareaであることが多い
        title_el = wait.until(EC.presence_of_element_located((By.TAG_NAME, "textarea")))
        title_el.click()
        title_el.send_keys(title)
        print("✅ タイトルを入力しました。")
    except Exception as e:
        print(f"⚠ タイトル欄が見つかりませんでした: {e}")
        print("   → `python note_auto_draft.py explore` を実行して、正しい要素を確認してください。")
        driver.quit()
        return

    try:
        # 本文欄: contenteditableな要素(タイトル欄の次の入力欄)
        editable = driver.find_elements(By.CSS_SELECTOR, "[contenteditable='true']")
        if not editable:
            raise Exception("contenteditable要素が見つかりません")
        body_el = editable[0]
        body_el.click()
        for line in body.split("\n"):
            body_el.send_keys(line)
            body_el.send_keys(Keys.ENTER)
        print("✅ 本文を入力しました。")
    except Exception as e:
        print(f"⚠ 本文欄への入力に失敗しました: {e}")
        print("   → `python note_auto_draft.py explore` を実行して、正しい要素を確認してください。")
        input(">>> 手動で確認後、Enterキーでブラウザを閉じます... ")
        driver.quit()
        return

    # 「下書き保存」ボタンを明示的にクリックする(自動保存に頼らない)
    time.sleep(1)
    saved = _click_save_draft(driver)
    if saved:
        print("✅ 「下書き保存」ボタンをクリックしました。")
    else:
        print("⚠ 「下書き保存」ボタンが見つかりませんでした。手動で保存してください。")
    input(">>> 確認できたら Enterキーを押すとブラウザを閉じます... ")
    driver.quit()


def cmd_explore_publish(note_url_or_id=None):
    """
    有料エリア設定〜価格入力〜公開ボタンのDOM構造を調査する(公開フロー自動化の準備用)。

    使い方:
      A) 引数なし: 新規の空の下書きから始める
           python note_auto_draft.py explore_publish
         1. ブラウザが開くので、タイトル・本文を入力し、本文の途中を選択して
            有料エリア(「続きを読むには」)を設定し、「公開に進む」を押して
            価格入力欄が表示されるところまで進める
         2. ターミナルに戻ってEnterキーを押す

      B) 引数あり: 既存の下書き(例えば昨日の記事)から始める
           python note_auto_draft.py explore_publish n7693a02d9d92
           python note_auto_draft.py explore_publish https://editor.note.com/notes/n7693a02d9d92/edit/
         指定したノートの編集画面を直接開くので、入力の手間なくすぐ
         「公開に進む」を押して価格入力欄まで進められる。

      いずれの場合も、準備ができたらEnterキーを押した時点のDOM構造を
      explore_publish_page.html に保存する。保存されたファイルと、
      ターミナルに表示される一覧をClaudeに送ってください。

    ⚠ 重要: このコマンド実行中、実際の「投稿」「公開」ボタンは絶対に押さないでください。
      あくまでDOM構造を調べるためだけの手順です。
    """
    driver = make_driver(headless=False)

    if note_url_or_id:
        # "n7693a02d9d92" のようなID単体でも、フルURLでも受け付ける
        note_id = note_url_or_id.strip()
        if note_id.startswith("http"):
            # https://editor.note.com/notes/n7693a02d9d92/edit/ 等からIDを抜き出す
            parts = [p for p in note_id.split("/") if p]
            note_id = next((p for p in parts if p.startswith("n") and len(p) > 8), note_id)
        target_url = f"https://editor.note.com/notes/{note_id}/edit/"
        driver.get(target_url)
    else:
        driver.get(NEW_NOTE_URL)
    time.sleep(3)
    print(f"現在のURL: {driver.current_url}")

    if "login" in driver.current_url:
        print("⚠ ログインしていないようです。先に `python note_auto_draft.py setup` を実行してください。")
        driver.quit()
        return

    print("\n" + "=" * 60)
    if note_url_or_id:
        print("既存の下書きを開きました。以下の操作を手動で行ってください:")
        print("  1. (まだ有料エリアが未設定なら)本文の途中を選択し、有料エリアを設定する")
    else:
        print("ブラウザが開きました。以下の操作を手動で行ってください:")
        print("  1. タイトル・本文を適当に入力する")
        print("  2. 本文の途中を選択し、有料エリア(「続きを読むには」)を設定する")
    print("  ⚠ 実際の「投稿」「公開」ボタンは絶対に押さないこと")
    print("=" * 60)
    input("\n>>> 有料エリアの設定が終わったらEnterキーを押してください"
          "(自動で「公開に進む」をクリックします)... ")

    # 【2026-09-04追加】「公開に進む」のクリックタイミングを人間の手作業に頼ると、
    # ここまで2回連続でクリック前にEnterを押してしまい、価格入力画面のDOMが
    # 撮れていなかった。ここからは自動でクリックする。
    try:
        publish_btn = None
        for el in driver.find_elements(By.TAG_NAME, "button"):
            if el.text.strip() == "公開に進む":
                publish_btn = el
                break
        if publish_btn is None:
            print("⚠ 「公開に進む」ボタンが見つかりませんでした。手動でクリックしてから、"
                  "もう一度Enterキーを押してください。")
            input(">>> クリックしたらEnterキーを押してください... ")
        else:
            publish_btn.click()
            print("✅ 「公開に進む」をクリックしました。画面が切り替わるのを待ちます...")
            time.sleep(3)
    except Exception as e:
        print(f"⚠ 「公開に進む」の自動クリックに失敗しました: {e}")
        print("  手動でクリックしてから、もう一度Enterキーを押してください。")
        input(">>> クリックしたらEnterキーを押してください... ")

    print(f"現在のURL(クリック後): {driver.current_url}")

    with open("explore_publish_page.html", "w", encoding="utf-8") as f:
        f.write(driver.page_source)
    print("✅ explore_publish_page.html にページソースを保存しました。")

    print("\n--- ボタン要素(テキストがあるもの全て) ---")
    buttons = driver.find_elements(By.TAG_NAME, "button")
    for i, el in enumerate(buttons):
        text = el.text.strip()
        if text:
            print(f"[{i}] text={text!r} class={el.get_attribute('class')!r} "
                  f"aria-label={el.get_attribute('aria-label')!r}")

    print("\n--- input要素(価格入力欄を探す) ---")
    inputs = driver.find_elements(By.TAG_NAME, "input")
    for i, el in enumerate(inputs):
        print(f"[{i}] type={el.get_attribute('type')!r} name={el.get_attribute('name')!r} "
              f"placeholder={el.get_attribute('placeholder')!r} "
              f"aria-label={el.get_attribute('aria-label')!r} value={el.get_attribute('value')!r}")

    # 【2026-09-04追加】価格欄がネイティブの<input>ではなく、独自コンポーネント
    # (スピンボタンやcontenteditableの数値欄)の可能性があるため、検索範囲を広げる。
    print("\n--- '円'や'¥'、価格らしきテキストを含む要素 ---")
    try:
        price_like = driver.find_elements(
            By.XPATH,
            "//*[contains(text(), '円') or contains(text(), '¥') or contains(@aria-label, '価格') or contains(@placeholder, '価格')]"
        )
        for i, el in enumerate(price_like):
            txt = el.text.strip().replace("\n", " ")
            print(f"[{i}] tag={el.tag_name} text={txt[:60]!r} class={el.get_attribute('class')!r} "
                  f"contenteditable={el.get_attribute('contenteditable')!r} "
                  f"role={el.get_attribute('role')!r}")
    except Exception as e:
        print(f"  (検索中にエラー: {e})")

    print("\n--- role='spinbutton' または contenteditable の要素(数値入力の可能性) ---")
    try:
        spin_like = driver.find_elements(By.CSS_SELECTOR, "[role='spinbutton'], [contenteditable='true']")
        for i, el in enumerate(spin_like):
            txt = el.text.strip().replace("\n", " ")
            print(f"[{i}] tag={el.tag_name} text={txt[:60]!r} class={el.get_attribute('class')!r} "
                  f"aria-label={el.get_attribute('aria-label')!r}")
    except Exception as e:
        print(f"  (検索中にエラー: {e})")

    # 【2026-09-04追加】価格欄は「有料」を選択した後にだけ表示される可能性が高いため、
    # is_paid=paid のラジオボタンを自動でクリックしてから、もう一度価格まわりを探索する。
    try:
        paid_radio = None
        for el in driver.find_elements(By.CSS_SELECTOR, "input[name='is_paid']"):
            if el.get_attribute("value") == "paid":
                paid_radio = el
                break
        if paid_radio is not None:
            driver.execute_script("arguments[0].click();", paid_radio)
            print("\n✅ 「有料」を選択しました。価格欄が表示されるはずなので、再度探索します...")
            time.sleep(1.5)

            with open("explore_publish_page_paid.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            print("✅ explore_publish_page_paid.html にページソースを保存しました。")

            print("\n--- 【有料選択後】input要素 ---")
            for i, el in enumerate(driver.find_elements(By.TAG_NAME, "input")):
                print(f"[{i}] type={el.get_attribute('type')!r} name={el.get_attribute('name')!r} "
                      f"placeholder={el.get_attribute('placeholder')!r} "
                      f"aria-label={el.get_attribute('aria-label')!r} value={el.get_attribute('value')!r}")

            print("\n--- 【有料選択後】'円'や'¥'、価格らしきテキストを含む要素 ---")
            for i, el in enumerate(driver.find_elements(
                    By.XPATH,
                    "//*[contains(text(), '円') or contains(text(), '¥') or contains(@aria-label, '価格') or contains(@placeholder, '価格')]")):
                txt = el.text.strip().replace("\n", " ")
                print(f"[{i}] tag={el.tag_name} text={txt[:60]!r} class={el.get_attribute('class')!r} "
                      f"contenteditable={el.get_attribute('contenteditable')!r} role={el.get_attribute('role')!r}")

            print("\n--- 【有料選択後】role='spinbutton' または contenteditable の要素 ---")
            for i, el in enumerate(driver.find_elements(By.CSS_SELECTOR, "[role='spinbutton'], [contenteditable='true']")):
                txt = el.text.strip().replace("\n", " ")
                print(f"[{i}] tag={el.tag_name} text={txt[:60]!r} class={el.get_attribute('class')!r} "
                      f"aria-label={el.get_attribute('aria-label')!r}")
        else:
            print("\n⚠ 「有料」のラジオボタンが見つかりませんでした。")
    except Exception as e:
        print(f"\n⚠ 「有料」選択の自動化に失敗しました: {e}")

    input("\n>>> 確認できたら Enterキーを押すとブラウザを閉じます(投稿ボタンは押さないこと)... ")
    driver.quit()


def _make_profile_driver(headless=False):
    """
    常に永続プロフィール(chrome_profile_note)でドライバーを作る。
    note_cookies.json の有無に関係なくプロフィールログインを使わせたい場面
    (Cookieエクスポート時)専用のヘルパー。make_driver()は逆に
    note_cookies.jsonがあればそちらを優先してしまうため、ここでは分けている。
    """
    options = webdriver.ChromeOptions()
    _clean_stale_profile_locks()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--profile-directory=Default")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,1000")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    chrome_path = os.environ.get("CHROME_PATH")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    if chrome_path:
        options.binary_location = chrome_path
    if chromedriver_path:
        service = Service(executable_path=chromedriver_path)
        return webdriver.Chrome(options=options, service=service)
    return webdriver.Chrome(options=options)


def cmd_export_cookies():
    """
    永続プロフィールでのログイン状態から note.com のCookieを書き出す。
    GitHub Actionsなど永続プロフィールを持てない環境では、ここで書き出した
    note_cookies.json の中身を使ってログインする(make_driver()参照)。

    【重要】note_cookies.json が既に存在していても無視して、必ず永続プロフィール
    でログインし直す。事前に `python note_auto_draft.py setup` でログイン済みで
    あること。Cookieには有効期限があるため、ログインが切れたらこのコマンドを
    再実行して作り直す必要がある。
    """
    import json
    driver = _make_profile_driver(headless=False)
    try:
        driver.get(NEW_NOTE_URL)
        time.sleep(3)
        print(f"現在のURL: {driver.current_url}")
        if "login" in driver.current_url:
            print("⚠ ログインしていないようです。先に `python note_auto_draft.py setup` を実行してください。")
            return
        cookies = driver.get_cookies()
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"✅ {len(cookies)}件のCookieを {os.path.abspath(COOKIES_FILE)} に書き出しました。")
        print("   ⚠ このファイルにはログイン用の秘密情報が含まれます。")
        print("     - リポジトリに直接コミットしないこと(アカウント乗っ取りのリスク)")
        print("     - GitHub Actionsで使うには、中身をGitHub Secretsに保存し、")
        print("       ワークフロー実行時にファイルとして書き出してから利用すること")
        print("     - Cookieの有効期限が切れたら、このコマンドを再実行して更新すること")
    finally:
        driver.quit()


def main():
    if len(sys.argv) < 2:
        print("使い方:")
        print("  python note_auto_draft.py setup            # 初回ログイン")
        print("  python note_auto_draft.py explore           # 画面構造の調査")
        print("  python note_auto_draft.py explore_publish    # 公開フロー(有料エリア・価格・公開ボタン)の調査")
        print("  python note_auto_draft.py post <記事ファイル>    # 新規下書き作成")
        print("  python note_auto_draft.py update <記事ファイル>  # 既存下書きを上書き更新(無ければ新規作成)")
        print("  python note_auto_draft.py export-cookies    # 永続プロフィールのログイン状態からCookieを書き出す(Actions用)")
        return

    cmd = sys.argv[1]
    if cmd == "setup":
        cmd_setup()
    elif cmd == "explore":
        cmd_explore()
    elif cmd == "explore_publish":
        cmd_explore_publish(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "post":
        if len(sys.argv) < 3:
            print("記事ファイルのパスを指定してください。例:")
            print("  python note_auto_draft.py post note_articles/note_article_20260802.txt")
            return
        cmd_post(sys.argv[2])
    elif cmd == "update":
        if len(sys.argv) < 3:
            print("記事ファイルのパスを指定してください。例:")
            print("  python note_auto_draft.py update note_articles/note_article_20260802.txt")
            return
        cmd_update(sys.argv[2], headless=False)
    elif cmd == "export-cookies":
        cmd_export_cookies()
    else:
        print(f"不明なコマンド: {cmd}")


if __name__ == "__main__":
    main()
