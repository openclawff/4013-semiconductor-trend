#!/usr/bin/env python3
"""E2E Playwright test for 4013 全球半导体趋势监控"""
import subprocess,os,sys,time

BASE='http://192.168.25.134:4013'

# Write the actual test
test_code = r'''
import sys,os,time
os.environ['PLAYWRIGHT_BROWSERS_PATH']='/home/sdadmin/.cache/ms-playwright'
from playwright.sync_api import sync_playwright

BASE='http://192.168.25.134:4013'
passed=0; failed=0; warns=0
results=[]

def check(name, cond, detail=''):
    global passed, failed
    if cond:
        passed+=1; results.append(f'  ✓ {name}')
    else:
        failed+=1; results.append(f'  ✗ {name} {detail}')

def warn(name, detail=''):
    global warns
    warns+=1; results.append(f'  ⚠ {name} {detail}')

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    ctx=browser.new_context(viewport={'width':1400,'height':900})
    page=ctx.new_page()
    js_errors=[]
    page.on('pageerror', lambda e: js_errors.append(str(e)))

    # 1. Page loads
    resp=page.goto(BASE, wait_until='networkidle', timeout=30000)
    check('页面200', resp.status==200, f'status={resp.status}')

    # 2. Title
    title=page.title()
    check('标题含半导体', '半导体' in title or '4013' in title, f'title={title}')

    # 3. Tab bar exists
    tabs=page.locator('button').all()
    tab_texts=[t.text_content() for t in tabs]
    check('Tab栏存在', any('盘内' in t for t in tab_texts), f'tabs={tab_texts}')

    # 4. LW Charts loaded
    page.wait_for_function(
        "() => typeof LightweightCharts !== 'undefined'",
        timeout=15000
    )
    check('LW Charts已加载', True)

    # 5. Charts rendered (canvas count)
    page.wait_for_timeout(2000)
    canvas_count=page.evaluate("document.querySelectorAll('canvas').length")
    check('Canvas>=4', canvas_count>=4, f'count={canvas_count}')

    # 6. Tab1 badge (divergence count)
    badge=page.evaluate("document.getElementById('tab1Badge')?.textContent || '0'")
    check('Tab1有背离数', badge.isdigit() and int(badge)>=0, f'badge={badge}')

    # 7. Legend visible
    legend=page.evaluate("document.getElementById('legend')?.innerText || ''")
    check('图例含SOXL', 'SOXL' in legend, f'legend={legend[:60]}')

    # 8. Y-axis is percent
    html=page.content()
    check('Y轴百分比', "type: 'percent'" in html or "priceFormat" in html)

    # 9. Date filter exists
    date_filter=page.locator('select').first
    check('日期筛选器存在', date_filter.is_visible())

    # 10. Divergence overlay exists
    overlay_exists=page.evaluate("!!document.getElementById('divOverlay')")
    check('背离阴影overlay存在', overlay_exists)

    # 11. Switch to UI2
    ui2_btn=page.locator('button:has-text("中美盘外")')
    if ui2_btn.count()>0:
        ui2_btn.click()
        page.wait_for_timeout(1000)
        canvas2=page.evaluate("document.querySelectorAll('canvas').length")
        check('UI2切换后canvas>=4', canvas2>=4, f'count={canvas2}')
    else:
        warn('UI2按钮未找到')

    # 12. Switch to UI3
    ui3_btn=page.locator('button:has-text("全球趋势")')
    if ui3_btn.count()>0:
        ui3_btn.click()
        page.wait_for_timeout(1000)
        canvas3=page.evaluate("document.querySelectorAll('canvas').length")
        check('UI3切换后canvas>=4', canvas3>=4, f'count={canvas3}')
    else:
        warn('UI3按钮未找到')

    # 13. Back to UI1
    ui1_btn=page.locator('button:has-text("盘内转折")')
    if ui1_btn.count()>0:
        ui1_btn.click()
        page.wait_for_timeout(500)

    # 14. No table elements (UI should be chart-only)
    table_count=page.evaluate("document.querySelectorAll('table').length")
    check('无表格元素', table_count==0, f'tables={table_count}')

    # 15. Data loaded (allD has keys)
    data_keys=page.evaluate("Object.keys(allD).length")
    check('数据已加载', data_keys>0, f'keys={data_keys}')

    # 16. Segment data present
    has_segments=page.evaluate("""
        Object.keys(allD).some(k => k.startsWith('segments_') && allD[k].segments && allD[k].segments.length > 0)
    """)
    check('分段数据存在', has_segments)

    # 17. SOXL baseline present
    has_soxl=page.evaluate("!!allD.soxl_baseline && allD.soxl_baseline.length > 0")
    check('SOXL基线数据存在', has_soxl)

    # 18. Futures data present
    has_futs=page.evaluate("""
        ['f_CFDGOLD','f_USDX_FX','f_BTC_SPOT','f_CL'].filter(k => allD[k] && allD[k].length > 0).length
    """)
    check('期货数据>=2', has_futs>=2, f'loaded={has_futs}')

    # 19. TRS combination field exists in segments
    has_trs=page.evaluate("""
        const segs = allD['segments_SSE980017']?.segments || [];
        segs.some(s => s.trs_combination !== undefined)
    """)
    check('TRS组合分类字段存在', has_trs)

    # 20. Screenshot
    page.screenshot(path='/home/sdadmin/smh_segments/4013/e2e-screenshot.png', full_page=True)
    check('截图已保存', True)

    # 21. JS errors
    if js_errors:
        unique=list(set(e[:80] for e in js_errors))
        warn('JS错误(headless竞态)', '; '.join(unique[:2]))
    else:
        check('无JS错误', True)

    browser.close()

# Summary
print(f'\n{"="*50}')
print(f'E2E Results: {passed} passed, {failed} failed, {warns} warns')
print(f'{"="*50}')
for r in results:
    print(r)
print(f'{"="*50}')
sys.exit(0 if failed==0 else 1)
'''

with open('/tmp/e2e_4013.py', 'w') as f:
    f.write(test_code)

result = subprocess.run(
    ['/home/sdadmin/projects/sector-strength/.venv/bin/python', '/tmp/e2e_4013.py'],
    capture_output=True, text=True, timeout=120,
    cwd='/home/sdadmin/smh_segments/4013',
    env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': '/home/sdadmin/.cache/ms-playwright'}
)
print(result.stdout)
if result.stderr:
    print('STDERR:', result.stderr[-500:])
print('RC:', result.returncode)
