import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(
            storage_state="data/instagram_session.json",
            viewport={"width": 1280, "height": 850}
        )
        page = await ctx.new_page()
        await page.goto("https://www.instagram.com/", timeout=40000)
        await page.wait_for_timeout(3000)

        # Find our profile link from the left sidebar
        # Often it's the profile image/link at the bottom left or in top nav
        profile_el = await page.query_selector('a:has(span:has-text("Profile")), a[href^="/"][role="link"]:has(img)')
        href = await profile_el.get_attribute("href") if profile_el else None
        print(f"Profile link found: {href}")

        target_url = f"https://www.instagram.com{href}" if href else "https://www.instagram.com/"
        await page.goto(target_url, timeout=40000)
        await page.wait_for_timeout(4000)
        await page.screenshot(path="tmp/profile_verification.png")
        print(f"Profile screenshot saved to tmp/profile_verification.png at URL: {page.url}")
        
        # Check text on page
        body = await page.evaluate("() => document.body.innerText")
        print("Page excerpt:", body[:300].replace('\n', ' '))
        await b.close()

if __name__ == "__main__":
    asyncio.run(check())
