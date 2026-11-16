// Behaviour checks for the todo_html task in a headless browser (deliverable-quality-001).
//
// Usage: node bench/todo_behaviour.cjs PAGE.html
// Prints one JSON object: {"checks": {name: bool, ...}} or {"unavailable": reason}.
// The page is opened from a file:// URL; every request that is not the page itself is aborted,
// so a page cannot reach the network (the spec forbids external resources anyway).
'use strict';

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (error) {
  console.log(JSON.stringify({ unavailable: 'playwright is not installed for node' }));
  process.exit(0);
}
const { pathToFileURL } = require('url');

const STEP_MS = 3000;

async function main(file) {
  const url = pathToFileURL(file).href;
  let browser;
  try {
    browser = await chromium.launch(process.env.ALETHEIA_CHROMIUM ? { executablePath: process.env.ALETHEIA_CHROMIUM } : {});
  } catch (error) {
    console.log(JSON.stringify({ unavailable: 'chromium could not start' }));
    return;
  }
  const checks = {};
  const errors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 375, height: 700 } });
    await context.route('**/*', route => (route.request().url() === url ? route.continue() : route.abort()));
    const page = await context.newPage();
    page.setDefaultTimeout(STEP_MS);
    page.on('pageerror', () => errors.push('pageerror'));
    await page.goto(url, { waitUntil: 'load' });

    const items = () => page.locator('.todo-item');
    const visible = () => page.evaluate(() => [...document.querySelectorAll('.todo-item')]
      .filter(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden').length);
    const stored = () => page.evaluate(() => {
      const strings = [];
      const walk = value => {
        if (typeof value === 'string') strings.push(value);
        else if (value && typeof value === 'object') Object.values(value).forEach(walk);
      };
      try { walk(JSON.parse(localStorage.getItem('todos'))); } catch (e) { /* not JSON */ }
      return strings;
    });
    const run = async (name, step) => {
      try { checks[name] = Boolean(await step()); } catch (error) { checks[name] = false; }
    };

    await run('enter_adds_trimmed', async () => {
      const before = await items().count();
      await page.fill('#new-todo', '  买牛奶  ');
      await page.press('#new-todo', 'Enter');
      const strings = await stored();
      return (await items().count()) === before + 1 && strings.includes('买牛奶') && !strings.includes('  买牛奶  ');
    });
    await run('button_adds', async () => {
      const before = await items().count();
      await page.fill('#new-todo', '交水费');
      await page.click('#add-btn');
      return (await items().count()) === before + 1;
    });
    await run('blank_not_added', async () => {
      const before = await items().count();
      await page.fill('#new-todo', '   ');
      await page.press('#new-todo', 'Enter');
      await page.fill('#new-todo', '   ');
      await page.click('#add-btn');
      return (await items().count()) === before;
    });
    await run('complete_strikethrough', async () => {
      await items().first().locator('input[type="checkbox"]').check();
      return page.evaluate(() => {
        const item = document.querySelector('.todo-item');
        return [item, ...item.querySelectorAll('*')].some(el =>
          getComputedStyle(el).textDecorationLine.includes('line-through'));
      });
    });
    await run('remaining_count_updates', async () =>
      (await page.textContent('#remaining-count')).trim() === '1');
    await run('filters_show_matching_items', async () => {
      await page.click('[data-filter="active"]');
      const active = await visible();
      await page.click('[data-filter="completed"]');
      const completed = await visible();
      await page.click('[data-filter="all"]');
      return active === 1 && completed === 1 && (await visible()) === 2;
    });
    await run('persists_after_reload', async () => {
      await page.reload({ waitUntil: 'load' });
      return (await items().count()) === 2
        && (await page.locator('.todo-item input[type="checkbox"]:checked').count()) === 1;
    });
    await run('delete_removes_item', async () => {
      await items().first().locator('.delete-btn').click();
      return (await items().count()) === 1;
    });
    await run('no_horizontal_scroll_375', async () =>
      page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    checks.no_script_errors = errors.length === 0;
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ checks }));
}

main(process.argv[2]).catch(() => {
  console.log(JSON.stringify({ unavailable: 'the browser check failed to run' }));
});
