"""
tools/login_instagram.py

Interactive helper to log into Instagram and save the session.
Opens a browser window on your desktop so you can log in, handle any 
passwords, 2FA, or security prompts. 

Once logged in, it automatically saves the session to `data/instagram_session.json`
so future autoposts run completely automatically without asking for credentials again.
"""

import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

SESSION_PATH = Path("data/instagram_session.json")

async def manual_login():
    username = os.getenv("INSTAGRAM_USERNAME", "")
    password = os.getenv("INSTAGRAM_PASSWORD", "")

    print("\n" + "=" * 60)
    print("  INSTAGRAM ONE-TIME SESSION LOGIN HELPER")
    print("=" * 60)
    print("  Opening browser on your desktop...")
    print("  You can log in directly in the browser window.")
    print("  Once you are logged in, this tool will detect it and save your session.")
    print("=" * 60 + "\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 850},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()
        await page.goto("https://www.instagram.com/accounts/login/", timeout=60000)

        # Pre-fill if credentials exist
        try:
            if username:
                u_inp = await page.wait_for_selector('input[name="email"], input[name="username"]', timeout=5000)
                if u_inp:
                    await u_inp.fill(username)
            if password:
                p_inp = await page.wait_for_selector('input[name="pass"], input[name="password"]', timeout=3000)
                if p_inp:
                    await p_inp.fill(password)
        except Exception:
            pass

        print("👉 Please check the browser window and complete login.")
        print("Waiting up to 300 seconds (5 minutes) for login to succeed...\n")

        # Poll every 2 seconds for successful login
        logged_in = False
        for _ in range(150):
            await page.wait_for_timeout(2000)
            try:
                # Check cookies for active session
                cookies = await context.cookies()
                has_session = any(c.get("name") == "sessionid" for c in cookies)

                # Check for home or create icons
                home_icon = await page.query_selector('svg[aria-label="Home"]')
                create_icon = await page.query_selector('svg[aria-label="New post"], svg[aria-label="Create"]')
                
                # Check URL
                cur_url = page.url
                if (has_session or home_icon or create_icon) and "/accounts/login" not in cur_url:
                    logged_in = True
                    break
            except Exception:
                continue

        if logged_in:
            SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(SESSION_PATH))
            print("\n" + "=" * 60)
            print("  ✅ SUCCESS! Instagram session saved to:")
            print(f"     {SESSION_PATH.resolve()}")
            print("  You can now run autoposts without entering credentials again!")
            print("=" * 60 + "\n")
        else:
            print("\n❌ Login timed out or was not completed. Please try again.")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(manual_login())
