// Run via Playwright MCP browser_run_code_unsafe(filename=absolute path), with web/ served on :8901.
async (page) => {
  const context = await page.context().browser().newContext();
  page = await context.newPage();
  try {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const origin = 'http://127.0.0.1:8901';
  const requests = [], posts = [];
  let responseStatus = 202, done = false, failed = false, missing = false, holdPost = null;
  const job = {job_id:'smoke123', preset:'restore', created_at:1793630000,
    status:'running', inputs:['/api/result/smoke123/input/0'], image_url:null};
  const current = () => ({...job, status:failed ? 'failed' : done ? 'done' : 'running',
    image_url:done ? '/api/result/smoke123/file' : null});
  await page.setViewportSize({width:375, height:812});
  await page.clock.install();
  await page.route('**/api/**', async route => {
    const req = route.request(), url = new URL(req.url());
    requests.push(req.url());
    assert(url.origin === origin && url.pathname.startsWith('/api/'), 'API escaped same origin');
    let data;
    if (url.pathname === '/api/config') data = {turnstile_sitekey:null};
    else if (url.pathname === '/api/credits') data = {credits_left:3};
    else if (url.pathname === '/api/presets') data = {presets:[
      {id:'background',label:'Background'}, {id:'clothes',label:'Clothes'},
      {id:'restore',label:'Restore'}, {id:'custom',label:'Custom'}]};
    else if (url.pathname === '/api/jobs') data = {jobs:posts.length ? [current()] : []};
    else if (url.pathname === '/api/edit') {
      posts.push(req.postData());
      if (holdPost) await holdPost;
      return route.fulfill({status:responseStatus, headers:{'Retry-After':'15'}, json:
        responseStatus === 202 ? {job_id:job.job_id, credits_left:2} : {detail:'Mock rejection'}});
    } else if (url.pathname.endsWith('/file') || url.pathname.endsWith('/input/0')) {
      return route.fulfill({contentType:'image/svg+xml', body:'<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512"><rect width="512" height="512" fill="tan"/></svg>'});
    } else if (missing) return route.fulfill({status:404,json:{detail:'unknown job'}});
    else data = {...current(), credits_left:2};
    return route.fulfill({json:data});
  });
  await page.addInitScript(() => {
    const original = window.fetch;
    window.fetch = (path, options) => {
      const url = new URL(path, location.href);
      if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) throw new Error('Non-API fetch');
      return original(path, options);
    };
  });
  await page.goto(origin);
  await page.locator('#presets button').last().waitFor();
  assert(await page.locator('#presets button').count() === 4, 'Missing presets');
  assert((await page.locator('#prompt').getAttribute('placeholder')).includes('Bisaya:'), 'Missing multilingual placeholder');
  const upload = () => page.evaluate(() => {
    const dt = new DataTransfer();
    dt.items.add(new File(['mock photo'], 'photo.png', {type:'image/png'}));
    const input = document.querySelector('#fileInput'); input.files = dt.files;
    input.dispatchEvent(new Event('change'));
  });
  await upload();
  await page.getByRole('button', {name:'Restore', exact:true}).click();
  assert(await page.locator('#prompt').inputValue() === '', 'Preset inserted sample instructions');
  let release;
  holdPost = new Promise(resolve => { release = resolve; });
  await page.locator('#submitBtn').click();
  await page.locator('#submitBtn:disabled').waitFor({state:'attached'});
  assert(posts.length === 1, 'Restore with empty prompt rejected');
  await page.evaluate(() => document.querySelector('#submitBtn').click());
  assert(posts.length === 1, 'Duplicate submission');
  release(); holdPost = null;
  await page.locator('#editAnother').waitFor({state:'visible'});
  assert(posts[0].includes('name="image"') && posts[0].includes('name="preset"'), 'Wrong multipart contract');
  const polls = () => requests.filter(url => new URL(url).pathname === '/api/result/smoke123').length;
  const count = polls();
  await page.clock.fastForward(9999);
  assert(polls() === count, 'Polling interval shorter than 10 seconds');
  done = true;
  await page.clock.fastForward(1);
  await page.locator('#result input[type=range]').waitFor();
  await page.locator('#result input[type=range]').fill('75');
  assert((await page.locator('#result .compare-before').getAttribute('style')).includes('25%'), 'Slider does not clip original');
  const download = page.waitForEvent('download');
  await page.locator('#result button').click();
  assert((await download).suggestedFilename() === 'free-photo-edit-smoke123.png', 'Wrong download');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile overflow');
  await page.reload();
  await page.locator('#result input[type=range]').waitFor();
  done = false;
  await page.reload();
  await page.locator('.job-spin').waitFor({state:'attached'});
  const resumed = polls();
  await page.clock.fastForward(10000);
  await page.waitForFunction(() => document.querySelector('#history .job-spin'));
  assert(polls() > resumed, 'Running job did not resume after reload');
  await upload();
  await page.locator('#prompt').fill('Make the background blue');
  for (const status of [402,403,429,502,503]) {
    responseStatus = status;
    await page.locator('#submitBtn').click();
    await page.locator('#submitBtn:not(:disabled)').waitFor();
    assert(await page.locator('#status').evaluate(el => el.classList.contains('error')), 'Missing error for '+status);
  }
  assert((await page.locator('#status').textContent()).includes('Mock rejection'), 'Server error discarded');
  await page.getByRole('button', {name:'Custom', exact:true}).click();
  await page.locator('#prompt').fill('');
  const beforeValidation = posts.length;
  await page.locator('#submitBtn').click();
  assert(posts.length === beforeValidation, 'Empty custom instruction submitted');
  await page.evaluate(() => {
    const files = new DataTransfer();
    for (let i=0;i<2;i++) files.items.add(new File(['x'],i+'.png',{type:'image/png'}));
    document.querySelector('#dropzone').dispatchEvent(new DragEvent('drop',{dataTransfer:files,bubbles:true,cancelable:true}));
  });
  assert((await page.locator('#status').textContent()).includes('exactly 1'), 'Multiple photos silently accepted');
  failed = true;
  await page.clock.fastForward(10000);
  await page.locator('#status').filter({hasText:'credit refunded'}).waitFor();
  failed = false;
  await page.reload();
  await page.locator('.job-spin').waitFor({state:'attached'});
  missing = true;
  await page.clock.fastForward(10000);
  await page.locator('#status').filter({hasText:'no longer available'}).waitFor();
  const stopped = polls();
  await page.clock.fastForward(20000);
  assert(polls() === stopped, 'Missing job still polled');
  await page.unrouteAll({behavior:'wait'});
  return {passed:true, checks:'mobile layout, presets, multilingual placeholder, empty restore, multipart 202, duplicate-submit guard, 10s polling, slider, download, completed/running resume, 402/403/429/502/503, empty custom, multi-photo rejection, async failure/refund, 404 poll stop, API-only fetch guard', apiRequests:requests.length, submissions:posts.length};
  } finally { await context.close(); }
}
