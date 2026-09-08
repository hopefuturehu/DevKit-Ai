import {defineConfig} from '@playwright/test';
// The readiness probe must reach the local fixture even with a system proxy.
process.env.NO_PROXY = [process.env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(',');
process.env.no_proxy = process.env.NO_PROXY;
const port=process.env.BOT_WEB_TEST_PORT||'8874';
export default defineConfig({
  testDir:'tests/web',testMatch:'*.spec.mjs',fullyParallel:false,workers:1,
  timeout:30000,expect:{timeout:10000},
  use:{baseURL:`http://127.0.0.1:${port}`,viewport:{width:1360,height:950},trace:'retain-on-failure',screenshot:'only-on-failure'},
  webServer:{command:'.venv/bin/python tests/web/serve_fixture.py',url:`http://127.0.0.1:${port}/api/status`,reuseExistingServer:false,timeout:30000},
});
