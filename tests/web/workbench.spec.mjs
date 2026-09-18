import {test,expect} from '@playwright/test';

async function ready(page){
  const sessions=await (await page.request.get('/api/sessions')).json();
  const seed=sessions.find(s=>s.title==='检查配置并执行验证');
  await page.goto('/');await expect(page.locator('#connection')).toHaveText('实时已连接');
  await page.evaluate(id=>localStorage.setItem('bot.session',id),seed.id);await page.reload();
  await expect(page.locator('#connection')).toHaveText('实时已连接');
  await expect(page.locator('#activity .tool-card').first()).toBeVisible();
}

test('history, tool input/output, safe text, artifacts, children and context',async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await ready(page);
  await expect(page.locator('#taskTitle')).toHaveText('检查配置并执行验证');
  await expect(page.locator('#planItems')).toContainText('检查配置');
  await expect(page.locator('.tool-button').filter({hasText:'update_plan'})).toContainText('已完成');
  await page.locator('.tool-button').filter({hasText:'run_command'}).click();
  await expect(page.locator('#detailBody')).toContainText('3 passed');
  await page.getByRole('button',{name:'参数',exact:true}).click();
  await expect(page.locator('#detailBody')).toContainText('argv');
  await page.getByRole('button',{name:'事件',exact:true}).click();
  await expect(page.locator('#detailBody')).toContainText('tool.requested');
  await expect(page.locator('body')).toContainText('<script>window.injected=true</script>');
  expect(await page.evaluate(()=>window.injected)).toBeUndefined();
  await page.getByRole('button',{name:'关闭详情'}).click();
  await page.getByRole('button',{name:'文件与产物',exact:true}).click();
  await page.locator('.artifact-row').filter({hasText:'result.txt'}).click();
  await expect(page.locator('#detailBody')).toContainText('+verified output');
  const download=page.waitForEvent('download');await page.getByRole('link',{name:'下载当前快照'}).click();expect((await download).suggestedFilename()).toBe('result.txt');
  await page.getByRole('button',{name:'关闭详情'}).click();
  await page.getByRole('button',{name:'子任务',exact:true}).click();
  await page.locator('.child-card').filter({hasText:'检查边界条件'}).click();
  await expect(page.locator('#activity')).toContainText('边界条件已检查。');
  await page.getByRole('button',{name:'返回主任务'}).click();
  await page.getByRole('button',{name:'上下文',exact:true}).click();
  await page.getByRole('button',{name:/查看摘要与原文/}).click();
  await expect(page.locator('#detailBody')).toContainText('已检查配置并完成验证。');
  await expect(page.locator('#detailBody')).toContainText('压缩前原文');
  expect(errors).toEqual([]);
});

test('task continues across reload and can receive steering and stop',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'新建会话'}).click();
  await page.locator('#prompt').fill('长任务：持续运行以检查连接恢复');await page.locator('#send').click();
  await expect(page.locator('#stop')).toBeVisible();
  await expect(page.locator('.tool-button').filter({hasText:'run_command'})).toBeVisible();
  await page.reload();await expect(page.locator('#connection')).toHaveText('实时已连接');
  await expect(page.locator('#taskTitle')).toContainText('长任务');
  await page.locator('#prompt').fill('请在完成时说明结果');await page.locator('#send').click();
  await expect(page.locator('#activity')).toContainText('请在完成时说明结果');
  await page.locator('.tool-button').filter({hasText:'run_command'}).click();
  await expect(page.locator('#detailBody')).toContainText('verification in progress');
  await page.keyboard.press('Escape');
  await expect(page.locator('.tool-button').filter({hasText:'run_command'})).toBeFocused();
  await page.locator('#stop').click();
  await expect(page.locator('#runMeta .status-chip')).toHaveText('已停止');
  await expect(page.locator('#stop')).toBeHidden();
  await expect(page.locator('#activity .tool-card').first()).toBeVisible();
});

test('reading older activity retains scroll position during live output',async({page})=>{
  await ready(page);await page.getByRole('button',{name:'新建会话'}).click();
  await page.locator('#prompt').fill('长任务：'+('阅读旧内容时保持位置。'.repeat(160)));
  await page.locator('#send').click();
  await expect(page.locator('.tool-button').filter({hasText:'run_command'})).toBeVisible();
  await page.locator('#activityScroller').evaluate(s=>s.scrollTop=100);
  await expect(page.locator('#followLatest')).toBeVisible();
  await expect.poll(()=>page.locator('#activityScroller').evaluate(s=>s.scrollTop)).toBe(100);
  // A real managed process emits another chunk while the user reads old content.
  await page.locator('.tool-button').filter({hasText:'run_command'}).evaluate(b=>b.click());
  await expect(page.locator('#detailBody')).toContainText('verification in progress');
  await expect.poll(()=>page.locator('#activityScroller').evaluate(s=>s.scrollTop)).toBe(100);
  await page.keyboard.press('Escape');await page.locator('#followLatest').click();
  await expect(page.locator('#followLatest')).toBeHidden();
  await page.locator('#stop').click();await expect(page.locator('#runMeta .status-chip')).toHaveText('已停止');
});

test('approval is attached to the correct tool and survives refresh',async({page,request})=>{
  await request.post('/__test__/approval/true');
  try{
    await ready(page);await page.getByRole('button',{name:'新建会话'}).click();
    await page.locator('#prompt').fill('检查审批流程');await page.locator('#send').click();
    await expect(page.getByRole('button',{name:'允许一次'})).toBeVisible();
    await page.reload();await expect(page.getByRole('button',{name:'允许一次'})).toBeVisible();
    await page.getByRole('button',{name:'允许一次'}).click();
    await expect(page.locator('#runMeta .status-chip')).toHaveText('已完成');
    await expect(page.getByRole('button',{name:'允许一次'})).toBeHidden();
  }finally{await request.post('/__test__/approval/false');}
});

test('analysis console lists runs, explains net duration and exports a report',async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/');await expect(page.locator('#connection')).toHaveText('实时已连接');
  await page.getByRole('button',{name:'历史分析',exact:true}).click();
  await expect(page.locator('#analysisPanel')).toBeVisible();

  // Methodology must be visible so the numbers are interpretable.
  await page.locator('#analysisMethod summary').click();
  await expect(page.locator('#analysisMethodBody')).toContainText('净耗时');
  await expect(page.locator('#analysisMethodBody')).toContainText('并集');

  // Cross-session list: the seeded runs come from three different sessions.
  const rows=page.locator('.analysis-row');
  await expect(rows.filter({hasText:'分析台验收：完成运行含审批等待'})).toBeVisible();
  await expect(rows.filter({hasText:'分析台验收：取消运行含未配对审批'})).toBeVisible();
  await expect(rows.filter({hasText:'分析台验收：缺少结束时间'})).toBeVisible();

  // Net duration = total - approval union: 100s total, 30s approval -> 70s net.
  const completed=rows.filter({hasText:'分析台验收：完成运行含审批等待'});
  await expect(completed).toContainText('1m 40s');
  await expect(completed).toContainText('30.0 s');
  await expect(completed).toContainText('1m 10s');

  // A run without an end time must read as unknown, never as 0.
  const open=rows.filter({hasText:'分析台验收：缺少结束时间'});
  await expect(open).toContainText('未知');

  // Stop-reason statistics are aggregated and labelled.
  await expect(page.locator('#analysisReasons')).toContainText('正常完成');
  await expect(page.locator('#analysisReasons')).toContainText('用户停止');

  // Detail view shows the breakdown and links to the trace.
  await completed.click();
  await expect(page.locator('#analysisDetail')).toBeVisible();
  await expect(page.locator('#analysisDetail')).toContainText('净耗时');
  await expect(page.locator('#analysisDetail')).toContainText('审批等待');
  await expect(page.locator('#analysisDetail')).toContainText('30.0 s');
  await page.getByRole('button',{name:'查看执行轨迹'}).click();
  await expect(page.locator('#activity')).toBeVisible();

  // A run without an end time must read as unknown in its detail view too.
  await page.getByRole('button',{name:'历史分析',exact:true}).click();
  await open.click();
  await expect(page.locator('#analysisDetail')).toContainText('缺少结束时间');
  await expect(page.locator('#analysisDetail')).toContainText('未知');

  // Back to the console, then export the JSON report.
  await page.getByRole('button',{name:'历史分析',exact:true}).click();
  const download=page.waitForEvent('download');
  await page.getByRole('button',{name:'导出 JSON'}).click();
  const file=await download;
  expect(file.suggestedFilename()).toMatch(/^run-analysis-.*\.json$/);
  const stream=await file.createReadStream();
  const chunks=[];for await(const chunk of stream)chunks.push(chunk);
  const report=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  expect(report.schema).toBe('bot.run-analysis.v1');
  expect(report.methodology.net_seconds).toContain('并集');
  expect(report.runs.length).toBeGreaterThanOrEqual(3);
  expect(errors).toEqual([]);
});

test('analysis console filters by stop reason and sorts by net duration',async({page})=>{
  await page.goto('/');await expect(page.locator('#connection')).toHaveText('实时已连接');
  await page.getByRole('button',{name:'历史分析',exact:true}).click();
  await expect(page.locator('.analysis-row').first()).toBeVisible();

  await page.locator('#filterReason').selectOption('cancelled');
  // Other tests may create cancelled runs too, so assert on the seeded one
  // rather than on an exact row count.
  const cancelledRows=page.locator('.analysis-row');
  await expect(cancelledRows.filter({hasText:'分析台验收：取消运行含未配对审批'})).toBeVisible();
  await expect(cancelledRows.filter({hasText:'分析台验收：完成运行含审批等待'})).toHaveCount(0);
  await expect(cancelledRows.filter({hasText:'分析台验收：取消运行含未配对审批'})).toContainText('1 次审批未配对');

  await page.locator('#filterReason').selectOption('');
  await page.locator('#filterSort').selectOption('net');
  await page.locator('#filterOrder').selectOption('asc');
  // Ascending net duration: the seeded 1m 10s run must precede the 10m cancelled run.
  const ascending=await page.locator('.analysis-row').allTextContents();
  const completedIndex=ascending.findIndex(t=>t.includes('分析台验收：完成运行含审批等待'));
  const cancelledIndex=ascending.findIndex(t=>t.includes('分析台验收：取消运行含未配对审批'));
  expect(completedIndex).toBeGreaterThanOrEqual(0);
  expect(cancelledIndex).toBeGreaterThan(completedIndex);
  // Unknown durations stay last in both directions.
  await page.locator('#filterOrder').selectOption('desc');
  await expect(page.locator('.analysis-row').last()).toContainText('分析台验收：缺少结束时间');
  await page.locator('#filterOrder').selectOption('asc');
  await expect(page.locator('.analysis-row').last()).toContainText('分析台验收：缺少结束时间');
});

for(const width of [360,736,1024])for(const colorScheme of ['light','dark']){
  test(`responsive ${width}px ${colorScheme}`,async({page},testInfo)=>{
    await page.setViewportSize({width,height:900});await page.emulateMedia({colorScheme});
    await page.goto('/');await expect(page.locator('#connection')).toHaveText('实时已连接');
    // New sessions from earlier tests may be selected; choose seeded history via API.
    const sessions=await (await page.request.get('/api/sessions')).json();const seed=sessions.find(s=>s.title==='检查配置并执行验证');
    await page.evaluate(id=>localStorage.setItem('bot.session',id),seed.id);await page.reload();
    await expect(page.locator('.tool-button').filter({hasText:'run_command'})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.locator('.tool-button').filter({hasText:'run_command'}).click();
    await expect(page.locator('#detailBody')).toContainText('3 passed');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await page.screenshot({path:testInfo.outputPath(`workbench-${width}-${colorScheme}.png`),fullPage:true,animations:'disabled'});
    await page.getByRole('button',{name:'关闭详情'}).click();
    await expect(page.locator('#prompt')).toBeVisible();
  });
}
