import os
import asyncio
from google import genai
from google.genai import types
from playwright.async_api import async_playwright
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# خواندن کلیدها از متغیرهای محیطی امن (GitHub Secrets)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

client = genai.Client(api_key=GEMINI_API_KEY)


async def open_browser_and_search(query: str) -> str:
    """باز کردن مرورگر ابری، جستجو و گرفتن اسکرین‌شات."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto("https://www.google.com", timeout=30000)
            await page.fill("textarea[name='q']", query)
            await page.press("textarea[name='q']", "Enter")
            await page.wait_for_timeout(3000)

            screenshot_path = "agent_result.png"
            await page.screenshot(path=screenshot_path)
            await browser.close()
            return f"جستجو برای '{query}' در محیط ابری انجام شد و تصویر صفحه آماده است."
        except Exception as e:
            await browser.close()
            return f"خطا در اجرای مرورگر ابری: {str(e)}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_message = update.message.text
    await update.message.reply_text("🤖 پیام شما دریافت شد؛ ایجنت ابری در حال بررسی است...")

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"درخواست کاربر: {user_message}\nاگر نیاز به باز کردن مرورگر، وب‌گردی یا بررسی سایت است از ابزار open_browser_and_search استفاده کن؛ در غیر این صورت پاسخ مستقیم و مفید بده.",
            config=types.GenerateContentConfig(
                tools=[open_browser_and_search],
                temperature=0.2,
            ),
        )

        if response.function_calls:
            for call in response.function_calls:
                if call.name == "open_browser_and_search":
                    query_arg = call.args.get("query", user_message)
                    await update.message.reply_text(
                        f"🌐 در حال اجرای مرورگر ابری برای: {query_arg}"
                    )
                    result = await open_browser_and_search(query_arg)
                    await update.message.reply_text(result)

                    if os.path.exists("agent_result.png"):
                        with open("agent_result.png", "rb") as photo:
                            await update.message.reply_photo(photo=photo)
        else:
            reply_text = response.text or "پاسخی تولید نشد."
            await update.message.reply_text(reply_text)

    except Exception as e:
        await update.message.reply_text(f"❌ خطا در پردازش: {str(e)}")


def main():
    if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN:
        raise ValueError("کلیدهای GEMINI_API_KEY یا TELEGRAM_BOT_TOKEN تنظیم نشده‌اند!")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    print("🚀 ایجنت ابری جمینی در گیت‌هاب روشن شد...")
    app.run_polling()


if __name__ == "__main__":
    main()
