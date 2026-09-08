
import sys, os, time
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/home/sdadmin/.cache/ms-playwright'
from playwright.sync_api import sync_playwright

BASE = 'http://192.168.25.134:4013'
passed = 0; failed = 0; errors = []

def check(name, cond, detail=''):
    global passed, failed
    if cond:
        passed += 1; print(f'  ✅ {name}')
    else:
        failed += 1; msg = f'  ❌ {name}'
        if detail: msg += f' — {detail}'
        print(msg); errors.append(name)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={'width': 1400, 'height': 900})
    page = ctx.new_page()
    js_errors = []
    page.on('pageerror', lambda e: js_errors.append(str(e)))

    resp = page.goto(BASE, timeout=30000)
    check('页面200', resp.status == 200)

    # Wait for LW Charts AND data to load
    page.wait_for_function(
        "() => typeof LightweightCharts !== 'undefined' && typeof allD !== 'undefined' && Object.keys(allD).length > 0",
        timeout=30000
    )
    page.wait_for_timeout(3000)

    # Check state
    state = page.evaluate("""
        JSON.stringify({
            lw: typeof LightweightCharts !== 'undefined',
            canvases: document.querySelectorAll('canvas').length,
            allDKeys: Object.keys(allD),
            chartsKeys: Object.keys(charts),
            legend: document.getElementById('legend')?.textContent?.substring(0, 100)
        })
    """)
    import json
    s = json.loads(state)
    print(f"  State: LW={s['lw']}, canvas={s['canvases']}, data={s['allDKeys'][:3]}, charts={s['chartsKeys']}")
    print(f"  Legend: {s['legend'][:80]}")

    check('Canvas>=2', s['canvases'] >= 2, f"count={s['canvases']}")
    check('数据已加载', len(s['allDKeys']) > 3, f"keys={s['allDKeys']}")
    check('图表已创建', len(s['chartsKeys']) >= 2, f"charts={s['chartsKeys']}")
    check('图例有内容', bool(s['legend']), f"legend={s['legend'][:50]}")

    # Tabs
    tabs = page.locator('div[onclick^="switchTab"]').all()
    check('3个Tab', len(tabs) == 3)

    # Tab2 switch
    tabs[1].click()
    page.wait_for_timeout(1000)
    ui2 = page.locator('#panel1.active').count() > 0
    check('UI2切换', ui2)

    # Tab3 switch
    tabs[2].click()
    page.wait_for_timeout(1000)
    ui3 = page.locator('#panel2.active').count() > 0
    check('UI3切换', ui3)

    # Percent format
    has_pct = page.evaluate("document.documentElement.innerHTML.includes(\"type:'percent'\")")
    check('Y轴百分比', has_pct)

    # No critical JS errors
    critical = [e for e in js_errors if 'ResizeObserver' not in e]
    check('无JS错误', len(critical) == 0, '; '.join(critical[:2]) if critical else '')

    # Date filter
    check('日期筛选器', page.locator('#dateFilter').count() > 0)
    check('刷新按钮', page.locator('button:has-text("🔄")').count() > 0)
    check('全范围按钮', page.locator('button:has-text("全范围")').count() > 0)

    browser.close()

print(f'\n{"="*50}')
print(f'Result: {passed}/{passed+failed} passed, {failed} failed')
if errors: print(f'Failed: {", ".join(errors)}')
sys.exit(0 if failed == 0 else 1)
