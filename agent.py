import asyncio
import base64
import importlib.util
import json
import os
import time
from typing import Any, Callable, Optional
from urllib import error as url_error
from urllib import request as url_request

from google import genai
from google.genai import types
from playwright.async_api import async_playwright
from telegram import Bot

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GH_TOKEN = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or ""
DEFAULT_SESSION_REPO = os.environ.get("SESSION_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""

# لیست مدل‌های فعال و جایگزین (بدون مدل‌های قدیمی)
CANDIDATE_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-pro",
]

# نگهداری وضعیت انتظار تعاملی در اجرای جاری (کپچا، رمز، کد تایید)
PENDING_STATE: dict[str, Any] = {
    "active": False,
    "expires_at": 0.0,
    "kind": "",          # مثلاً "captcha_or_input" یا "credentials"
    "url": "",
    "session_key": "",
    "selector": "",      # انتخابگر اختیاری فیلد ورودی؛ اگر خالی باشد تلاش هوشمند انجام می‌شود
    "prompt_note": "",
}


async def send_telegram_message(bot: Bot, chat_id: str, text: str) -> None:
    if not chat_id or not text:
        return
    for i in range(0, len(text), 3900):
        await bot.send_message(chat_id=chat_id, text=text[i : i + 3900])


async def send_telegram_photo(bot: Bot, chat_id: str, photo_path: str, caption: str = "") -> None:
    if not chat_id or not os.path.exists(photo_path):
        return
    with open(photo_path, "rb") as photo_file:
        await bot.send_photo(chat_id=chat_id, photo=photo_file, caption=caption[:1000])


def _clear_old_screenshot() -> None:
    if os.path.exists("agent_result.png"):
        try:
            os.remove("agent_result.png")
        except Exception:
            pass


# -------------------- ابزارهای گیت‌هاب و ذخیره نشست --------------------

def github_read_file(repo: str, file_path: str, branch: str = "main") -> str:
    """خواندن محتوای یک فایل متنی یا JSON از مخزن گیت‌هاب (فرمت repo: owner/repo_name)."""
    if not GH_TOKEN:
        return "توکن گیت‌هاب (GH_PAT یا GITHUB_TOKEN) تنظیم نشده است."
    if not repo or not file_path:
        return "نام مخزن (repo) و مسیر فایل (file_path) الزامی است."

    api_url = f"https://api.github.com/repos/{repo}/contents/{file_path}?ref={branch}"
    req = url_request.Request(
        api_url,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Cloud-AI-Agent",
        },
    )
    try:
        with url_request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("encoding") == "base64" and data.get("content"):
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
                return content
            return f"فایل یافت شد اما محتوای متنی قابل خواندن نداشت: {file_path}"
    except url_error.HTTPError as e:
        return f"خطای HTTP در خواندن فایل از گیت‌هاب ({e.code}): {e.reason}"
    except Exception as e:
        return f"خطا در خواندن فایل از گیت‌هاب: {str(e)}"


def github_write_file(repo: str, file_path: str, content: str, commit_message: str, branch: str = "main") -> str:
    """ساخت یا به‌روزرسانی یک فایل در مخزن گیت‌هاب و ثبت Commit."""
    if not GH_TOKEN:
        return "توکن گیت‌هاب (GH_PAT یا GITHUB_TOKEN) تنظیم نشده است."
    if not repo or not file_path:
        return "نام مخزن (repo) و مسیر فایل (file_path) الزامی است."

    api_url = f"https://api.github.com/repos/{repo}/contents/{file_path}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Cloud-AI-Agent",
        "Content-Type": "application/json",
    }

    sha: Optional[str] = None
    get_req = url_request.Request(f"{api_url}?ref={branch}", headers=headers)
    try:
        with url_request.urlopen(get_req, timeout=20) as resp:
            existing = json.loads(resp.read().decode("utf-8"))
            sha = existing.get("sha")
    except Exception:
        sha = None

    payload: dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    put_req = url_request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="PUT",
    )
    try:
        with url_request.urlopen(put_req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            commit_sha = (result.get("commit") or {}).get("sha", "")[:7]
            return f"فایل {file_path} در مخزن {repo} با موفقیت ذخیره شد (Commit: {commit_sha})."
    except url_error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return f"خطای HTTP در ذخیره فایل در گیت‌هاب ({e.code}): {body[:400]}"
    except Exception as e:
        return f"خطا در ذخیره فایل در گیت‌هاب: {str(e)}"


def _load_session_state_from_github(session_key: str) -> Optional[str]:
    """در صورت وجود نشست ذخیره‌شده در گیت‌هاب، آن را به فایل محلی موقت می‌نویسد و مسیرش را برمی‌گرداند."""
    if not session_key or not DEFAULT_SESSION_REPO or not GH_TOKEN:
        return None
    safe_key = "".join(ch for ch in session_key if ch.isalnum() or ch in ("-", "_")).strip("_-")
    if not safe_key:
        return None
    raw = github_read_file(DEFAULT_SESSION_REPO, f"sessions/{safe_key}.json")
    if raw.startswith("{"):
        local_path = f"/tmp/session_{safe_key}.json"
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(raw)
        return local_path
    return None


def _save_session_state_to_github(session_key: str, local_path: str) -> str:
    """ذخیره فایل storage_state مرورگر در مخزن گیت‌هاب برای استفاده‌های بعدی."""
    if not session_key:
        return "کلید نشست (session_key) مشخص نشده است."
    if not DEFAULT_SESSION_REPO or not GH_TOKEN:
        return "برای ذخیره دائمی نشست، تنظیم GH_PAT و مخزن (SESSION_REPO یا GITHUB_REPOSITORY) لازم است."
    if not os.path.exists(local_path):
        return "فایل نشست محلی یافت نشد."
    safe_key = "".join(ch for ch in session_key if ch.isalnum() or ch in ("-", "_")).strip("_-")
    if not safe_key:
        return "نام کلید نشست نامعتبر است."

    with open(local_path, "r", encoding="utf-8") as f:
        state_content = f.read()

    return github_write_file(
        repo=DEFAULT_SESSION_REPO,
        file_path=f"sessions/{safe_key}.json",
        content=state_content,
        commit_message=f"Save browser session state for {safe_key}",
    )


# -------------------- ابزارهای مرورگر و تعامل با کپچا/ورود --------------------

async def open_browser_and_search(query: str) -> str:
    """فقط زمانی که کاربر صراحتاً جستجوی مرورگری یا تصویر صفحه گوگل بخواهد استفاده شود."""
    _clear_old_screenshot()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        try:
            await page.goto("https://www.google.com", timeout=30000)
            await page.wait_for_timeout(1500)

            search_box = page.locator("textarea[name='q'], input[name='q']").first
            await search_box.fill(query)
            await search_box.press("Enter")
            await page.wait_for_timeout(3500)

            await page.screenshot(path="agent_result.png", full_page=False)
            title = await page.title()
            body_text = await page.inner_text("body")
            snippet = " ".join(body_text.split())[:1500]
            await browser.close()
            return f"جستجوی مرورگر برای '{query}' انجام شد. عنوان: {title}. متن: {snippet}"
        except Exception as e:
            try:
                await page.screenshot(path="agent_result.png", full_page=False)
            except Exception:
                pass
            await browser.close()
            return f"خطا در جستجوی مرورگر برای '{query}': {str(e)}"


async def open_url(
    url: str,
    session_key: str = "",
    save_session: bool = False,
    wait_for_user_input: bool = False,
    timeout_seconds: int = 180,
    input_note: str = "",
) -> str:
    """
    باز کردن مستقیم یک آدرس در مرورگر ابری.
    - session_key: در صورت وجود نشست ذخیره‌شده با این نام، از آن استفاده می‌کند.
    - save_session: در صورت True بودن، وضعیت نشست را برای مراجعات بعدی ذخیره می‌کند.
    - wait_for_user_input: در صورت وجود کپچا، کد تایید یا نیاز به ورود دستی، وضعیت انتظار را با مهلت زمانی فعال می‌کند.
    """
    _clear_old_screenshot()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    loaded_state_path = _load_session_state_from_github(session_key) if session_key else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        if loaded_state_path and os.path.exists(loaded_state_path):
            context_kwargs["storage_state"] = loaded_state_path

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(3500)

            if save_session and session_key:
                tmp_state = f"/tmp/save_{session_key}.json"
                await context.storage_state(path=tmp_state)
                _save_session_state_to_github(session_key, tmp_state)

            await page.screenshot(path="agent_result.png", full_page=False)
            title = await page.title()
            body_text = await page.inner_text("body")
            snippet = " ".join(body_text.split())[:2500]
            await browser.close()

            if wait_for_user_input:
                PENDING_STATE["active"] = True
                PENDING_STATE["expires_at"] = time.monotonic() + max(30, min(timeout_seconds, 300))
                PENDING_STATE["kind"] = "user_input"
                PENDING_STATE["url"] = url
                PENDING_STATE["session_key"] = session_key
                PENDING_STATE["selector"] = ""
                PENDING_STATE["prompt_note"] = input_note or "کد کپچا، کد تایید یا اطلاعات موردنیاز را ارسال کنید."

            session_note = f" (نشست: {session_key})" if session_key else ""
            return f"صفحه {url}{session_note} باز شد. عنوان: {title}. محتوای متنی: {snippet}"
        except Exception as e:
            try:
                await page.screenshot(path="agent_result.png", full_page=False)
            except Exception:
                pass
            await browser.close()
            return f"خطا در باز کردن آدرس {url}: {str(e)}"


async def submit_pending_input_to_url(
    url: str,
    user_value: str,
    session_key: str = "",
    save_session: bool = False,
) -> str:
    """ارسال کد کپچا، رمز یا مقدار واردشده توسط کاربر به فرم صفحه مقصد و ثبت نتیجه."""
    _clear_old_screenshot()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    loaded_state_path = _load_session_state_from_github(session_key) if session_key else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        if loaded_state_path and os.path.exists(loaded_state_path):
            context_kwargs["storage_state"] = loaded_state_path

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(2000)

            visible_inputs = page.locator("input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
            count = await visible_inputs.count()
            if count == 0:
                await page.screenshot(path="agent_result.png", full_page=False)
                await browser.close()
                return f"در صفحه {url} فیلد متنی قابل پر کردن پیدا نشد."

            # اگر چند فیلد وجود داشت و مقدار با کاما یا خط جدید جدا شده بود، به‌ترتیب پر می‌کند
            parts = [part.strip() for part in user_value.replace("\n", ",").split(",") if part.strip()]
            if len(parts) > 1 and count >= len(parts):
                for idx, val in enumerate(parts):
                    await visible_inputs.nth(idx).fill(val)
            else:
                await visible_inputs.first.fill(user_value.strip())

            await visible_inputs.last.press("Enter")
            await page.wait_for_timeout(3500)

            save_msg = ""
            if save_session and session_key:
                tmp_state = f"/tmp/save_{session_key}.json"
                await context.storage_state(path=tmp_state)
                save_msg = " | " + _save_session_state_to_github(session_key, tmp_state)

            await page.screenshot(path="agent_result.png", full_page=False)
            title = await page.title()
            body_text = await page.inner_text("body")
            snippet = " ".join(body_text.split())[:2000]
            await browser.close()
            return f"مقدار در {url} ثبت شد{save_msg}. عنوان جدید: {title}. متن صفحه: {snippet}"
        except Exception as e:
            try:
                await page.screenshot(path="agent_result.png", full_page=False)
            except Exception:
                pass
            await browser.close()
            return f"خطا در ثبت مقدار در {url}: {str(e)}"


# -------------------- لودر داینامیک پلاگین‌ها از پوشه plugins/ --------------------

def load_dynamic_plugins(plugins_dir: str = "plugins") -> list[Callable[..., Any]]:
    """بارگذاری خودکار توابع ابزار از فایل‌های .py داخل پوشه plugins/ در صورت وجود."""
    loaded_tools: list[Callable[..., Any]] = []
    if not os.path.isdir(plugins_dir):
        return loaded_tools

    for filename in sorted(os.listdir(plugins_dir)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        file_path = os.path.join(plugins_dir, filename)
        module_name = f"plugin_{filename[:-3]}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "get_tools") and callable(module.get_tools):
                    tools_from_mod = module.get_tools()
                    if isinstance(tools_from_mod, list):
                        for fn in tools_from_mod:
                            if callable(fn):
                                loaded_tools.append(fn)
        except Exception as err:
            print(f"خطا در بارگذاری پلاگین {filename}: {err}")

    return loaded_tools


# -------------------- فراخوانی جمینی با سوییچ خودکار مدل --------------------

def generate_with_fallback(
    client: genai.Client,
    contents: Any,
    config: Optional[types.GenerateContentConfig] = None,
) -> tuple[types.GenerateContentResponse, str]:
    """ارسال درخواست به مدل‌ها با سوییچ خودکار در صورت خطا یا شلوغی سرور."""
    last_error: Optional[Exception] = None

    for model_name in CANDIDATE_MODELS:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            return resp, model_name
        except Exception as err:
            last_error = err
            continue

    raise RuntimeError(f"هیچ‌یک از مدل‌های موجود پاسخ ندادند. آخرین خطا: {last_error}")


async def process_user_message(bot: Bot, client: genai.Client, chat_id: str, user_message: str) -> None:
    # اگر در بازه زمانی فعال، منتظر کپچا/کد تایید/اطلاعات ورود از سمت کاربر هستیم
    if PENDING_STATE["active"] and time.monotonic() <= PENDING_STATE["expires_at"]:
        target_url = PENDING_STATE["url"]
        session_key = PENDING_STATE["session_key"]
        PENDING_STATE["active"] = False

        await send_telegram_message(bot, chat_id, "⏳ در حال ثبت مقدار ارسالی در صفحه و بررسی نتیجه...")
        result_text = await submit_pending_input_to_url(
            url=target_url,
            user_value=user_message,
            session_key=session_key,
            save_session=bool(session_key),
        )
        if os.path.exists("agent_result.png"):
            await send_telegram_photo(bot, chat_id, "agent_result.png", caption="نتیجه پس از ثبت ورودی")
            _clear_old_screenshot()

        summary_resp, used_model = generate_with_fallback(
            client=client,
            contents=f"نتیجه ثبت ورودی در صفحه:\n{result_text}\n\nوضعیت را به زبان فارسی و کوتاه گزارش بده.",
            config=types.GenerateContentConfig(temperature=0.2),
        )
        await send_telegram_message(bot, chat_id, f"{(summary_resp.text or result_text).strip()}\n\n⚙️ مدل فعال: {used_model}")
        return
    elif PENDING_STATE["active"] and time.monotonic() > PENDING_STATE["expires_at"]:
        PENDING_STATE["active"] = False

    await send_telegram_message(bot, chat_id, "🤖 پیام شما دریافت شد؛ ایجنت ابری در حال پردازش است...")

    base_tools: list[Callable[..., Any]] = [
        open_browser_and_search,
        open_url,
        github_read_file,
        github_write_file,
    ]
    plugin_tools = load_dynamic_plugins("plugins")
    all_tools = base_tools + plugin_tools
    tool_map: dict[str, Callable[..., Any]] = {fn.__name__: fn for fn in all_tools if hasattr(fn, "__name__")}

    system_instruction = (
        "تو یک دستیار پژوهشی، تحلیلی و ایجنت خودکار هستی.\n"
        "قوانین رفتاری:\n"
        "1. برای تحقیق، تحلیل، بررسی علمی/دارویی/فنی یا گزارش‌های تحلیلی، بدون باز کردن مرورگر و بدون ارسال اسکرین‌شات پاسخ بده "
        "و در پایان بخش «منابع و مراجع معتبر» ذکر کن؛ اگر جزئیات یا منبعی قطعی نیست یا نیاز به راستی‌آزمایی دارد، شفاف بگو و منبع جعلی نساز.\n"
        "2. فقط زمانی از ابزار open_url استفاده کن که کاربر صراحتاً بخواهد وارد آدرس/سایت خاصی شود یا کاری در آن صفحه انجام دهد.\n"
        "3. فقط زمانی از ابزار open_browser_and_search استفاده کن که کاربر صراحتاً جستجوی مرورگری یا تصویر صفحه بخواهد.\n"
        "4. برای خواندن یا نوشتن فایل در مخازن گیت‌هاب از github_read_file و github_write_file استفاده کن.\n"
        "5. اگر پلاگین اضافی در ابزارها وجود داشت، در صورت نیاز مرتبط از همان ابزار استفاده کن."
    )

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=all_tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
    )

    response, used_model = generate_with_fallback(
        client=client,
        contents=user_message,
        config=config,
    )

    function_calls = getattr(response, "function_calls", None) or []

    if function_calls:
        for fc in function_calls:
            fn_name = fc.name
            fn_args = dict(fc.args or {})
            is_browser_tool = fn_name in ("open_url", "open_browser_and_search")

            if fn_name in tool_map:
                target_fn = tool_map[fn_name]
                if fn_name == "open_url":
                    await send_telegram_message(bot, chat_id, f"🌐 ورود به سایت در مرورگر ابری:\n{fn_args.get('url', '')}")
                elif fn_name == "open_browser_and_search":
                    await send_telegram_message(bot, chat_id, f"🔎 جستجوی مرورگری برای:\n{fn_args.get('query', user_message)}")

                if asyncio.iscoroutinefunction(target_fn):
                    tool_result = await target_fn(**fn_args)
                else:
                    tool_result = target_fn(**fn_args)
            else:
                tool_result = f"ابزار '{fn_name}' در دسترس نیست."

            if is_browser_tool and os.path.exists("agent_result.png"):
                await send_telegram_photo(
                    bot,
                    chat_id,
                    "agent_result.png",
                    caption=f"تصویر مرورگر (مدل: {used_model})",
                )
                _clear_old_screenshot()

            if PENDING_STATE["active"]:
                sec_left = int(max(1, PENDING_STATE["expires_at"] - time.monotonic()))
                await send_telegram_message(
                    bot,
                    chat_id,
                    f"⏸️ {PENDING_STATE['prompt_note']}\n"
                    f"مهلت پاسخ‌گویی در این اجرای فعال: حدود {sec_left} ثانیه.",
                )

            summary_prompt = (
                f"درخواست کاربر: {user_message}\n"
                f"خروجی ابزار ({fn_name}):\n{tool_result}\n\n"
                "گزارشی روشن، دقیق و کاربردی به زبان فارسی بنویس و در صورت وجود، منابع معتبر یا آدرس بررسی‌شده را ذکر کن."
            )
            summary_response, summary_model = generate_with_fallback(
                client=client,
                contents=summary_prompt,
                config=types.GenerateContentConfig(temperature=0.2),
            )
            final_text = (summary_response.text or str(tool_result)).strip()
            await send_telegram_message(bot, chat_id, f"{final_text}\n\n⚙️ مدل فعال: {summary_model}")
    else:
        reply_text = (response.text or "پاسخی از مدل دریافت نشد.").strip()
        await send_telegram_message(bot, chat_id, f"{reply_text}\n\n⚙️ مدل فعال: {used_model}")


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        raise ValueError("متغیرهای TELEGRAM_BOT_TOKEN یا GEMINI_API_KEY تنظیم نشده‌اند.")

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    client = genai.Client(api_key=GEMINI_API_KEY)

    offset: Optional[int] = None
    end_time = time.monotonic() + 240

    if ALLOWED_CHAT_ID:
        await send_telegram_message(bot, ALLOWED_CHAT_ID, "✅ ایجنت ابری روشن شد و آماده دریافت دستور است.")

    while time.monotonic() < end_time:
        updates = await bot.get_updates(offset=offset, timeout=15)
        for upd in updates:
            offset = upd.update_id + 1
            msg = upd.message
            if not msg or not msg.text:
                continue
            chat_id = str(msg.chat_id)
            if ALLOWED_CHAT_ID and chat_id != str(ALLOWED_CHAT_ID):
                continue
            await process_user_message(bot, client, chat_id, msg.text.strip())

        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
