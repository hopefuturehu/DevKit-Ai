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
