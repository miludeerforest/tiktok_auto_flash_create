import asyncio
import os
import sys
from playwright.async_api import async_playwright


def resolve_cdp_endpoint() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()

    port = os.getenv("CDP_PORT", "9222").strip()
    return f"http://127.0.0.1:{port}"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(resolve_cdp_endpoint())
        for ctx in browser.contexts:
            for page in ctx.pages:
                u = page.url or ''
                if 'seller' not in u.lower():
                    continue

                # Check main page for captcha elements
                has_captcha = await page.evaluate('''() => {
                    const sels = ['[class*="captcha" i]','[class*="verify" i]','[class*="secsdk" i]',
                                  '[class*="slider" i]','[class*="geetest" i]',
                                  'iframe[src*="captcha" i]','iframe[src*="verify" i]'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) return s + ' ' + el.tagName + ' ' + (el.className||'').substring(0,100);
                        }
                    }
                    return null;
                }''')
                if not has_captcha:
                    continue

                print(f'PAGE: {u[:80]}')
                print(f'  Main page hit: {has_captcha}')

                # List all iframes
                frames = page.frames
                print(f'  Total frames: {len(frames)}')
                for i, frame in enumerate(frames):
                    fu = frame.url or ''
                    fname = frame.name or ''
                    print(f'  Frame[{i}]: name={fname[:40]} url={fu[:100]}')

                    # Check each frame for slider/drag elements
                    try:
                        inner = await frame.evaluate('''() => {
                            const results = [];
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                const cls = (typeof el.className === 'string') ? el.className : '';
                                const tag = el.tagName;
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0 &&
                                    (cls.match(/captcha|verify|slider|secsdk|puzzle|drag|slide/i) ||
                                     tag === 'CANVAS' ||
                                     (tag === 'IMG' && rect.width > 30))) {
                                    results.push({
                                        tag: tag,
                                        cls: cls.substring(0, 120),
                                        w: Math.round(rect.width),
                                        h: Math.round(rect.height),
                                        x: Math.round(rect.x),
                                        y: Math.round(rect.y),
                                        src: tag === 'IMG' ? (el.src || '').substring(0, 100) : '',
                                    });
                                }
                            }
                            return results;
                        }''')
                        if inner:
                            for item in inner:
                                line = f'    {item["tag"]:8} {item["w"]:4}x{item["h"]:<4} @({item["x"]},{item["y"]}) cls={item["cls"][:80]}'
                                print(line)
                                if item.get('src'):
                                    print(f'             src={item["src"]}')
                    except Exception as e:
                        print(f'    (frame eval failed: {str(e)[:60]})')

        await browser.close()

asyncio.run(main())
