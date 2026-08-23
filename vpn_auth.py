import asyncio, subprocess, sys, time, os
from playwright.async_api import async_playwright

VPN_URL  = "https://vpn.uni.lu/MFA"
VPN_USER = os.getenv("VPN_USER", "aleksandr.koviazin@uni.lu")
VPN_PASS = os.getenv("VPN_PASS", "Ecosystem4005334")

async def get_cookie(totp_code: str) -> str | None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print("→ Открываю VPN страницу...")
        await page.goto(VPN_URL, wait_until="networkidle")

        print("→ Ввожу email...")
        await page.fill('input[type="email"], input[name="loginfmt"]', VPN_USER)
        await page.click('input[type="submit"], button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        print("→ Ввожу пароль...")
        await page.fill('input[type="password"], input[name="passwd"]', VPN_PASS)
        await page.click('input[type="submit"], button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        print("→ Ввожу TOTP код...")
        # Microsoft Authenticator — поле для кода
        await page.fill(
            'input[name="otc"], input[placeholder*="code"], input[placeholder*="код"]',
            totp_code
        )
        await page.click('input[type="submit"], button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        # Ждём редиректа обратно на vpn.uni.lu
        try:
            await page.wait_for_url("*vpn.uni.lu*", timeout=15000)
        except:
            # Может быть "Stay signed in?" экран
            try:
                await page.click('input[value="No"], button:has-text("No")', timeout=3000)
                await page.wait_for_url("*vpn.uni.lu*", timeout=10000)
            except:
                pass

        # Извлекаем cookie
        cookies = await page.context.cookies()
        await browser.close()

        for c in cookies:
            if c["name"] in ("webvpn", "acCookie", "ACOOKIE"):
                print(f"→ Cookie получен: {c['name']}")
                return c["value"]

        # Если cookie не в браузере — пробуем из URL
        url = page.url
        print(f"→ Финальный URL: {url}")
        return None


def vpn_connect(cookie: str) -> bool:
    if os.path.exists("/tmp/openconnect.pid"):
        pid = open("/tmp/openconnect.pid").read().strip()
        subprocess.run(["sudo", "kill", pid], capture_output=True)
        time.sleep(1)

    subprocess.Popen([
        "sudo", "openconnect",
        "--protocol=anyconnect",
        f"--cookie={cookie}",
        "--background",
        "--pid-file=/tmp/openconnect.pid",
        "--log-level=0",
        VPN_URL
    ])
    time.sleep(5)

    import httpx
    try:
        return httpx.get("http://gpu2.sedan.pro:11434/api/tags", timeout=3).status_code == 200
    except:
        return False


async def main():
    code = input("Введи 6-значный код из Microsoft Authenticator: ").strip()
    cookie = await get_cookie(code)

    if not cookie:
        print("❌ Не удалось получить cookie")
        print("Попробуй запустить с headless=False для отладки")
        return

    print("→ Подключаю VPN...")
    if vpn_connect(cookie):
        print("✅ VPN подключён, Ollama доступна")
    else:
        print("⚠️  VPN подключён но Ollama недоступна")


if __name__ == "__main__":
    asyncio.run(main())
